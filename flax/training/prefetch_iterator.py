# Lint as: python3

# Copyright 2020 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Utility for constructing an iterator which prefetches data asynchronously.
"""

import threading


class PrefetchIterator:
  """Wraps an iterator to provide async prefetching.

  This class is particularly useful for making data loading using TensorFlow
  more efficient. Currently, the numpy iterator in TensorFlow will not
  automatically prefetch data in the background::

    tf_iter = dataset.as_numpy_iterator()  # only loads data while calling next
    tf_iter = PrefetchIterator(tf_iter)  # prefetches data in the background

  """

  def __init__(self, data_iter, buffer_size=1):
    """Construct a PrefetchIterator.

    Args:
      data_iter: the Iterator that should be prefetched.
      buffer_size: how many items to prefetch (default: 1).
    """
    self._data_iter = data_iter
    self.buffer_size = buffer_size
    self._cond = threading.Condition()
    self._buffer = []
    self._active = True
    self._thread = threading.Thread(target=self._prefetch_loop, daemon=True)
    self._thread.start()
    self._error = None

  def __iter__(self):
    return self

  def __next__(self):
    with self._cond:
      self._cond.wait_for(lambda: self._buffer or not self._active)
      if self._buffer:
        item = self._buffer.pop(0)
        self._cond.notifyAll()
        return item
      if self._error:
        raise self._error  # pylint: disable=raising-bad-type
      assert not self._active
      raise StopIteration()

  def close(self):
    with self._cond:
      self._active = False
      self._cond.notifyAll()

  def _prefetch_loop(self):
    """Prefetch loop that prefetches a tf dataset."""
    def _predicate():
      return len(self._buffer) < self.buffer_size or not self._active

    while True:
      try:
        item = next(self._data_iter)
        with self._cond:
          self._buffer.append(item)
          self._cond.notifyAll()
          self._cond.wait_for(_predicate)
          if not self._active:
            return
      except Exception as e:  # pylint: disable=broad-except
        with self._cond:
          self._error = e
          self._active = False
          self._cond.notifyAll()
          return
