# Copyright 2020 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Linen: a refined Flax."""
from contextlib import contextmanager
import dataclasses
import functools
import inspect
import os
import threading
from typing import (Any, Callable, Sequence, Iterable, List, Optional, Tuple,
                    Set, Type, Union, TypeVar, Generic)

import jax
from jax import tree_util
import numpy as np

import flax
from flax import traverse_util
from flax import serialization
from flax.core import Scope, apply
from flax.core.scope import Variable
from flax.core.frozen_dict import freeze

# from .dotgetter import DotGetter

PRNGKey = Any  # pylint: disable=invalid-name
Array = Any    # pylint: disable=invalid-name
T = TypeVar('T')

# pylint: disable=protected-access,attribute-defined-outside-init

def _check_omnistaging():
  if not jax.config.omnistaging_enabled:
    raise RuntimeError(
        "Flax linen API requires JAX omnistaging to be enabled:\n"
        "  from jax.config import config\n"
        "  config.enable_omnistaging()")


def _indent(x: str, num_spaces: int):
  indent_str = ' ' * num_spaces
  lines = x.split('\n')
  # skip last line because it is always empty and should not be indented.
  assert lines[-1] == ''
  return '\n'.join(indent_str + line for line in lines[:-1]) + '\n'


def _attr_repr(value: Any):
  if isinstance(value, Callable) and getattr(value, '__name__', None):
    value_rep = value.__name__
  else:
    value_rep = repr(value)
  return value_rep

def _module_repr(module: 'Module', num_spaces: int = 4):
  """Returns a pretty printed representation of the module"""
  cls = type(module)
  cls_name = cls.__name__
  rep = ''
  attributes = {k: v for k, v in cls.__annotations__.items() if k not in ('parent', 'name')}
  child_modules = {k: v for k, v in module.children.items() if isinstance(v, Module)}
  if attributes:
    rep += '# attributes\n'
    for attr in attributes.keys():
      # TODO(jheek): can we get a nice string representation of attribute types?
      value = getattr(module, attr)
      value_rep = _attr_repr(value)
      rep += f'{attr} = {value_rep}\n'
  if child_modules:
    rep += '# children\n'
    for name, child in child_modules.items():
      child_rep = module_repr(child, num_spaces)
      rep += f'{name} = {child_rep}\n'
  if rep:
    return f'{cls_name}(\n{_indent(rep, num_spaces)})'
  else:
    return f'{cls_name}()'

# Track parent relationship across Modules.
# -----------------------------------------------------------------------------
class _DynamicContext:
  # TODO: switch to using contextvars once minimum python version is 3.7
  def __init__(self):
    self._thread_data = threading.local()
  @property
  def module_stack(self):
    if not hasattr(self._thread_data, 'module_stack'):
      self._thread_data.module_stack = [None,]
    return self._thread_data.module_stack

# The global context 
_context = _DynamicContext()

class _Sentinel:
  pass
_unspecified_parent = _Sentinel()


# Enable automatic named_call wrapping for labelling profile traces.
# -----------------------------------------------------------------------------
_use_named_call = True if os.getenv('FLAX_PROFILE', '') else False

def enable_named_call():
  """Enables named call wrapping for labelling profile traces."""
  global _use_named_call
  _use_named_call = True

def disable_named_call():
  """Disables named call wrapping."""
  global _use_named_call
  _use_named_call = False


# Utilities for autonaming pytrees of Modules defined inside setup()
# -----------------------------------------------------------------------------
def _get_suffix_value_pairs(
    tree_or_leaf: Any) -> List[Tuple[str, Type["Module"]]]:
  """Helper for naming pytrees of submodules."""
  dict_or_leaf = serialization.to_state_dict(tree_or_leaf)
  if dict_or_leaf == {} or not isinstance(dict_or_leaf, dict):
    return [('', tree_or_leaf)]
  else:
    flat_dict = traverse_util.flatten_dict(dict_or_leaf)
    return [('_' + '_'.join(k), v) for k, v in flat_dict.items()]

def _is_module_tree(in_tree: Any) -> bool:
  """Determines if `in_tree` is a pytree of subclasses of Module.

  Args:
    in_tree: Python object, typically a python tree.

  Returns:
    True if `in_tree` is non-empty and all leafs are Module, False otherwise.
  """
  # reject trivial pytrees, {}, [], (), etc.
  if not tree_util.tree_leaves(in_tree):
    return False
  reduce_fn = lambda prev, cur: prev and isinstance(cur, Module)
  return jax.tree_util.tree_reduce(reduce_fn, in_tree, True)


def _all_names_on_object(obj: Any) -> Set[str]:
  """Gets all names of attributes on `obj` and its classes throughout MRO.
  
  Args:
    obj: The object to get names for.
  Returns:
    A set of names of attributes of `obj` and its classes.
  """
  nameset = set(obj.__dict__.keys())
  for cls in obj.__class__.__mro__:
    nameset = nameset.union(set(cls.__dict__.keys()))
  return nameset


# Method wrapping of "compact methods" and setup()
# -----------------------------------------------------------------------------
def compact(fun: Callable) -> Callable:
  """Marks a single module method allowing inline submodules. 
  
  Methods wrapped in @compact can define submodules directly within the method.

  For instance:
    @compact
    __call__(self, x, features):
      x = nn.Dense(features)(x)
      ...
  
  At most one method in each Module may be wrapped with @compact.

  Args:
    fun: The Module method to mark as compact.
  Returns:
    The given function `fun` marked as compact.
  """
  fun.compact = True
  return fun


def _get_local_method_names(cls: Any, exclude: Tuple[str] = ()) -> Tuple[str]:
  """Gets method names of a class, excluding class and static methods.
  
  Args:
    cls: The class to get method names for.
    excludes: Names to exclude from output.
  Returns:
    A list of method names.
  """
  true_methods = set()
  for m in cls.__dict__:
    if callable(cls.__dict__[m]):
      mtype = type(cls.__dict__[m])
      if mtype != staticmethod and mtype != classmethod:
        true_methods.add(m)
  return tuple(true_methods.difference(set(exclude)))


def wrap_method(fun: Callable) -> Callable:
  """Manages Module state for user-defined methods.
  
  Args:
    fun: User-defined Module method to manage state for.
  Returns:
    Wrapped method.
  """
  @functools.wraps(fun)
  def wrapped_module_method(self, *args, **kwargs):
    is_compact_method = hasattr(fun, 'compact')
    is_setup_method = fun.__name__ == 'setup'

    if self.scope is None:
      raise ValueError("Can't call methods on orphaned modules")

    if is_compact_method:
      self._state.in_compact_method = True
    elif is_setup_method:
      self._state.in_setup = True
    _context.module_stack.append(self)
    try:
      return fun(self, *args, **kwargs)
    finally:
      _context.module_stack.pop()
      if is_compact_method:
        object.__setattr__(self, 'scope', self.scope.rewound())
      if is_compact_method or is_setup_method:
        self._state.reset()

  return wrapped_module_method


def _wrap_hash(hash_fn: Callable[..., Any]) -> Callable[..., Any]:
  @functools.wraps(hash_fn)
  def wrapped(self):
    if self.scope is not None:
      raise ValueError('Can\'t call __hash__ on modules that hold variables.')
    return hash_fn(self)
  return wrapped


def _get_unbound_fn(method_or_fn: Callable[..., Any]) -> Callable[..., Any]:
  """Return an unbound function from a method that is possibly bound.
  
  This means that the returned function does no longer depend on the instance
  of the class, which is passed as it first argument. 

  Args:
    method_or_fn: A class method or function.
  Returns:
    An unbound version of input function.
  """
  if inspect.ismethod(method_or_fn):
    return method_or_fn.__func__
  elif callable(method_or_fn):
    return method_or_fn
  else:
    raise ValueError('Expect a function or method.')


@dataclasses.dataclass
class _ModuleInternalState:
  """Ephemeral Module Evaluation State.

  For clarity, we collect all of the temporary flags and ephemeral state used by
  Modules for autonaming and error messages here.
  """
  in_compact_method: bool = False
  in_setup: bool = False
  last_varname: Optional[str] = None
  autoname_cursor: Optional[dict] = dataclasses.field(default_factory=dict)

  def reset(self):
    self.in_compact_method = False
    self.in_setup = False
    self.last_varname = None
    self.autoname_cursor = dict()

_uninitialized_module_internal_state = _ModuleInternalState(
    False, False, None, None)


# Base Module definition.
# -----------------------------------------------------------------------------
class Module:
  """Base class for all neural network modules.

  Your layers and modules should subclass this class.

  Modules are Python 3.7 
  `dataclasses <https://docs.python.org/3/library/dataclasses.html>`_. Since
  dataclasses override `__init__`, you should instead implement `setup` in
  your modules (which we call automatically).

  Modules can contain submodules, and in this way can be nested in a tree
  structure. Submodels can be assigned as regular attributes using the
  `setup` method.

  You can define arbitrary "forward pass" methods on your Module subclass.
  In particular, defining a `__call__` method allows for concise code when
  using module instances.

  ```
  from flax import nn as linen

  class Module(nn.Module):
    features: int = [16, 4]

    def setup(self):
      self.dense1 = Dense(self.features[0])
      self.dense2 = Dense(self.features[1])

    def __call__(self, x):
      return self.dense2(nn.relu(self.dense1(x)))
  ```

  Optionally, for more concise module implementaions where submodules 
  definitions are co-located with their usage, you can use the 
  :meth:`module.compact` wrapper.
  """

  @classmethod
  def __init_subclass__(cls):
    """Automatically initializes all subclasses as custom dataclasses."""
    # All Flax Modules are dataclasses.  We force this convention since
    # it encourages the stateless behavior needed to clone module instances for
    # functional transformation.  Instead of using a python metaclass, we
    # automatically transform Modules into dataclasses at subclass creation
    # time, and we set the last dataclass arguments to `parent` and `name`.
    cls._customized_dataclass_transform()
    # We wrap user-defined methods including setup and __call__ to enforce
    # a number of different checks and to provide clear error messages.
    cls._verify_single_or_no_compact()
    cls._wrap_module_methods()
    # Set empty class defaults.
    cls._state = _uninitialized_module_internal_state
    cls.scope = None

  @classmethod
  def _customized_dataclass_transform(cls):
    """Handle final optional dataclass attributes: `parent` and `name`."""
    # Use cls.__dict__ to get annotations of cls itself (no parent class).
    annotations = dict(cls.__dict__.get('__annotations__', {}))
    if 'parent' in annotations or 'name' in annotations:
      raise ValueError(
          f'properties `parent` and `name` are reserved: {annotations}')
    # Add `parent` and `name` default fields at end.
    # We temporarily modify base class __dataclass_fields__ to force desired
    # argument behavior and ordering from dataclass class-transform.
    parent_dataclass_fields = dict(getattr(cls, '__dataclass_fields__', {}))
    # Remove 'parent' and 'name' from parents because we always want parent and
    # name to show up last in the dataclass args.
    if 'parent' in parent_dataclass_fields:
      cls.__dataclass_fields__.pop('parent')
    if 'name' in parent_dataclass_fields:
      cls.__dataclass_fields__.pop('name')
    annotations['parent'] = Union[Type["Module"], Type["Scope"],
                                  Type["_Sentinel"], None]
    cls.parent = dataclasses.field(repr=False, default=_unspecified_parent)
    annotations['name'] = str
    cls.name = None  # default value of name is None.
    cls.__annotations__ = annotations
    # Now apply dataclass transform (which operates in-place).
    dataclasses.dataclass(cls, unsafe_hash=True, repr=False)
    cls.__hash__ = _wrap_hash(cls.__hash__)
    # Restore original base class __dataclass_fields__.
    if dataclasses.is_dataclass(cls.__bases__[0]):
      cls.__bases__[0].__dataclass_fields__ = parent_dataclass_fields

  @classmethod
  def _verify_single_or_no_compact(cls):
    """Statically verifies that at most a single method is labelled compact."""
    methods = [m[0] for m in inspect.getmembers(cls, predicate=callable)]
    n_compact_fns = len([method_name for method_name in methods
                         if hasattr(getattr(cls, method_name), 'compact')])
    if n_compact_fns > 1:
      raise RuntimeError(
          'Only one method per class can be @compact. You can remove @compact '
          'and define submodules and variables in setup(), or use two '
          'separate modules.')

  @classmethod
  def _wrap_module_methods(cls):
    """Wrap user-defined non-inherited methods with state management functions."""
    exclusions = ([f.name for f in dataclasses.fields(cls)] +
                  ['__eq__', '__repr__', '__init__', '__hash__'])
    for key in _get_local_method_names(cls, exclude=exclusions):
      method = getattr(cls, key)
      if _use_named_call and key != 'setup':
        # We import named_call at runtime to avoid a circular import issue.
        from flax.linen.transforms import named_call  # pylint: disable=g-import-not-at-top
        method = named_call(method)
      setattr(cls, key, wrap_method(method))
    return cls

  def __setattr__(self, name: str, val: Any):
    """Sets the an attribute on this Module.
    
    We overload setattr solely to support pythonic naming via assignment of 
    submodules in the special setup() function::
      self.submodule_name = MyModule(...)

    We also support lists and other general pytrees, e.g.::
      self.submodules = [MyModule0(..), MyModule1(..), ...]

    Args:
      name: Attribute to set.
      val: Value of the attribute.
    """
    # We don't mess with the parent module.
    if name == 'parent':
      pass
    # Modules have been passed in as dataclass args.
    elif name in self.__dataclass_fields__.keys():
      pass
    # Submodules are being defined and attached in setup()
    else:
      for suffix, subvalue in get_suffix_value_pairs(val):
        if isinstance(subvalue, Module):
          if not self._state.in_setup:
            raise ValueError(
                "You can only assign submodules to self in setup().")
          if subvalue.parent is _unspecified_parent:
            subvalue.parent = self
          elif subvalue.parent != self:
            raise ValueError("Can't attach to remote parent in setup, pass in "
                             "bound Modules from outside as an argument.")
          if subvalue.name is not None:
            raise ValueError(
                "In setup, assign names of Modules via self.<name> and not "
                "using keyword argument name=\"<name>\"")
          subvalue.name = f'{name}{suffix}'
          subvalue.__post_init__()
        # val is a parameter array or a Variable reference class.
        elif isinstance(subvalue, (np.ndarray, jax.interpreters.xla.DeviceArray,
                                   Variable)) and self._state.in_setup:
          var_name = f'{name}{suffix}'
          # namecheck to ensure named variable matches self attribute name.
          if self._state.last_varname and self._state.last_varname != var_name:
            raise ValueError(f'Variable name {self._state.last_varname} must '
                             f'equal attribute name {var_name}.')
          self._state.last_varname = None
    # Finally, always run default __setattr__ to attach to self.__dict__.
    object.__setattr__(self, name, val)

  def __post_init__(self):
    _check_omnistaging()
    # In dataclasses, __init__ is overridden to process dataclass arguments,
    # and __post_init__ is called immediately afterwards. Here, depending on the
    # type of `parent` passed to initialize the Module, we either defer 
    # initialization, attach this Module as a submodule of a parent, or bind
    # this Module at the top-level to variables and rngs.

    self._state = _ModuleInternalState()
    self.children = dict()  # tracks child modules

    # Typically we set the parent based on the dynamic module context.
    if self.parent is _unspecified_parent:
      self.parent = _context.module_stack[-1]

    # Initialization is deferred for top level Modules or any other "orphan"
    # Modules until attachment by __setattr__ i.e. MyModule(..., parent=None)
    if self.parent is None:
      return

    # Register submodule on parent Module.
    if isinstance(self.parent, Module):
      # When initializing an unnamed Module inside setup()
      # initialization is deferred until attachment by __setattr__
      # i.e. self.mymodule = MyModule(...)
      if self.parent._state.in_setup and self.name is None:
        return
      if not self.parent._initialization_allowed:
        raise ValueError(
            'Submodules must be defined in `setup()` or in a method wrapped '
            'in `@compact`')
      # Autonaming of submodules.
      if self.name is None:
        prefix = f"{self.__class__.__name__}"
        cursor = self.parent._state.autoname_cursor.get(prefix, 0)
        self.name = f"{prefix}_{cursor}"
        self.parent._state.autoname_cursor[prefix] = cursor + 1
      if self.parent._name_taken(self.name):
        raise ValueError(
            f"A variable of name {self.name} exists already, or "
            f"trying to share submodule {self.__class__.__name__} by name "
            f"{self.name}. To share submodules, store module instances as a"
            f" Python object or as an attribute on self and reuse.")
      self.parent.children[self.name] = self
      self.scope = self.parent.scope.push(self.name)

    # Top-level invocation with a functional Scope.
    elif isinstance(self.parent, Scope):
      self.scope = self.parent

    else:
      raise ValueError("parent must be None, Module or Scope")

    # Call the user-defined initialization setup() function.
    self.setup()

  def __repr__(self):
    return _module_repr(self)

  def setup(self):
    """Initializes a Module (similar to __init__ for non-dataclass Python classes).

    Override this method in your module subclasses to initialize submodules and
    other attributes. This method is called after all dataclass attributes are
    assigned and the module is ready for use. Variables and RNGs are guaranteed
    to be available.
    """
    pass

  def _name_taken(self, name: str) -> bool:
    return (name in self.scope.reservations or
            name in _all_names_on_object(self))

  @property
  def _initialization_allowed(self):
    return self._state.in_setup or self._state.in_compact_method

  def clone(self, *,
            parent: Optional[Union[Scope, 'Module']] = None,
            **updates) -> 'Module':
    """Create a clone of this Module, with optionally updated arguments.
    
    Args:
      parent: The parent of the clone. The clone will have no parent if no 
        explicit parent is specified.
      **updates: attribute updates.
    Returns:
      A clone of the this Module with the updated attributes and parent.
    """
    attrs = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
    attrs.update(parent=parent, **updates)
    return self.__class__(**attrs)

  def variable(self, col: str, name: str, init_fn, *init_args) -> Variable:
    """Declares a variable in this Module. Variables are mutable jax.numpy arrays
    that are stored a variable dict associated with this Module. See also the
    `variables` method.
    
    Args:
      col: The variable collection name. Each collection may or may not be
        mutable and different collections can be treated differently in JAX
        transformations.
        TODO: Make "variable collection" design note, and link to it from here.
      name: The variable name.
      init_fn: The function that will be called to compute the initial value
        of this variable. This function will only be called the first time
        this variable is used in this module.
      *init_args: The arguments to pass to init_fn.

    Returns:
      A :class:`scope.Variable` that can be read or set via ".value" attribute.

      TODO: Extract Variable into variable.py, link to that
      from this docstring.
    """
    if not self._initialization_allowed:
      raise ValueError(
          'Variables must be initialized in `setup()` or in a method '
          'wrapped in `@compact`')
    if self._name_taken(name):
      raise ValueError(
          f'Name {name} already in use in {self.__class__.__name__}.')
    # ephemeral state for setattr name-equality-check
    self._state.last_varname = name
    v = self.scope.variable(kind, name, init_fn, *init_args)
    self.children[name] = kind
    return v

  def param(self, name: str, init_fn: Callable[..., T], *init_args) -> T:
    """Declare a parameter in this Module. Parameters are read-only variables
    in the collection named "params". See `variable` for more details on
    module variables.

    Args:
      name: The parameter name.
      init_fn: The function that will be called to compute the initial value
        of this variable. This function will only be called the first time
        this variable is used in this module.
      *init_args: The arguments to pass to init_fn.

    Returns:
      The value of the initialized parameter.
    """
    if not self._initialization_allowed:
      raise ValueError(
          'Parameters must be initialized in `setup()` or in a method '
          'wrapped in `@compact`')
    if self._name_taken(name):
      raise ValueError(
          f'Name {name} already in use in {self.__class__.__name__}.')
    # ephemeral state for setattr name-equality-check
    self._state.last_varname = name
    v = self.scope.param(name, init_fn, *init_args)
    self.children[name] = 'params'
    return v

  def has_variable(self, kind: str, name: str):
    """Check if a variable of given kind and name exists in this Module."""
    return self.scope.has_variable(kind, name)

  def make_rng(self, kind: str) -> PRNGKey:
    """Get a new rng key of a given kind from this Module."""
    return self.scope.make_rng(kind)

  def apply(self, variables, *args, rngs=None,
            method: Callable[..., Any] = None, mutable=False, **kwargs):
    """Apply module to variables and return output and modified variables."""
    if method is None:
      method = self.__class__.__call__
    else:
      method = _get_unbound_fn(method)
    fn = lambda scope: method(self.clone(parent=scope),
                              *args, **kwargs)
    return apply(fn, mutable=mutable)(variables, rngs=rngs)

  def init_with_output(self, rngs, *args, method=None, **kwargs):
    """Create initialized data for module and return it with output."""
    if not isinstance(rngs, dict):
      assert rngs.shape == (2,)
      rngs = {'params': rngs}
    return self.apply(
        {}, *args, rngs=rngs, method=method, mutable=True, **kwargs)

  def init(self, rngs, *args, method=None, **kwargs):
    """Create and return initialized data for module with rngs."""
    _, v_out = self.init_with_output(rngs, *args, method=method, **kwargs)
    return v_out


  @property
  def variables(self):
    return self.scope.variables()


  # @contextmanager
  # def mutate(self, mutable=True, **updates):
  #   cloned = self.clone(**updates)
  #   try:
  #     cloned.scope._variables = _unfreeze_variables(
  #         cloned.scope._variables, mutable)
  #     yield cloned
  #   finally:
  #     cloned.scope._variables = freeze(cloned.scope._variables)

  # def initialized(self, rngs, *args, method='__call__', **kwargs):
  #   if self.parent is not None:
  #     raise ValueError("Pattern for initialized is "
  #                      "`Module(parent=None, ...attrs...).initialized(...)`")
  #   scope = Scope(variables={}, rngs=rngs)
  #   with self.mutate(parent=scope) as initialized:
  #     if method is not None:
  #       getattr(initialized, method)(*args, **kwargs)
  #   return initialized

  # @property
  # def variables(self):
  #   """Get a view of Module variables with easy dot-syntax navigation."""
  #   return DotGetter(self.scope.variables())

  # def __getattr__(self, name):
  #   # Used for easy colab/jupyter introspection, and to provide a
  #   # consistent top-level interface to self.<attr> for both simple
  #   # and multi-method modules.
  #   if name in self.children:
  #     val = self.children[name]
  #     if isinstance(val, str):  # variable
  #       return self.variables[val][name]
  #     else:  # submodule
  #       val.scope = self.scope.push(name)
  #       self.scope.reservations.remove(name)
  #       return val
  #   else:
  #     raise AttributeError(
  #         f"'{self.__class__.__name__}' object has no attribute '{name}'")

  # def __dir__(self):
  #   return list(self.children.keys()) + object.__dir__(self)

  # TODO: Should this be what `clone` always does if you don't pass in an explicit
  # parent?
  # def detached(self):
  #   return self.clone(parent=None)

  # # TODO: Consider whether this is a helpful abstraction, and think about naming.
  # # See its use in design_test/linen/weight_std.py
  # def materialized(self, variables={}, rngs={}):
  #   assert self.scope is None, ("Can't attach a module twice."
  #                               " Maybe you want to clone first?")
  #   return self.clone(parent=Scope(variables, rngs))
