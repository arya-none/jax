from functools import reduce
import operator as op
from typing import Callable

import numpy as np

from jax import core
from jax import lax
from jax import linear_util as lu
from jax.api_util import flatten_fun_nokwargs
from jax.tree_util import tree_flatten, tree_unflatten
from jax._src import source_info_util
from jax._src.util import safe_map, safe_zip, curry

source_info_util.register_exclusion(__file__)

map, unsafe_map = safe_map, map
zip, unsafe_zip = safe_zip, zip

NO_ERROR = np.iinfo(np.int32).max

class NanTracer(core.Tracer):
  __slots__ = ['val']

  def __init__(self, trace, val):
    self._trace = trace
    self.val = val

  aval = property(lambda self: core.get_aval(self.val))

class NanTrace(core.Trace):
  pure = lift = sublift = lambda self, val: NanTracer(self, val)

  def process_primitive(self, primitive, tracers, params):
    vals_in = [t.val for t in tracers]
    val_out = primitive.bind(*vals_in, **params)
    self.add_check(primitive, val_out)
    if primitive.multiple_results:
      return [NanTracer(self, x) for x in val_out]
    return NanTracer(self, val_out)

  def process_call(self, call_primitive, f, tracers, params):
    vals_in = [t.val for t in tracers]
    f = nancheck_subtrace(f, self.main)
    idx, *vals_out = call_primitive.bind(f, *vals_in, **params)
    self.main.idx = idx
    return [NanTracer(self, val) for val in vals_out]

  def add_check(self, primitive, val_out):
    main = self.main
    if not isinstance(val_out, (list, tuple)):
      val_out = [val_out]
    idx = lax.min(main.idx, len(main.backtraces))
    for x in val_out:
      main.idx = lax.select(jnp.any(x != x), idx, main.idx)
    main.backtraces.append((source_info_util.current(), str(primitive)))

def _any(lst): return reduce(op.or_, lst, False)

@curry
def nancheck(fun: Callable, *args):
  args, in_tree = tree_flatten(args)
  fun, out_tree = flatten_fun_nokwargs(lu.wrap_init(fun), in_tree)
  out_flat = nancheck_flat(fun, *args)  # type: ignore
  return tree_unflatten(out_tree(), out_flat)

def nancheck_flat(fun: lu.WrappedFun, *args):
  fun, backtraces = nancheck_fun(nancheck_subtrace(fun))
  idx, outs = fun.call_wrapped(*args)
  if idx != NO_ERROR:
    backtrace, name = backtraces()[idx]
    summary = source_info_util.summarize(backtrace)
    print(f"first nan generated by primitive {name} at {summary}")
  return outs

@lu.transformation_with_aux
def nancheck_fun(*args):
  backtraces = []
  with core.new_main(NanTrace) as main:
    main.idx, main.backtraces = NO_ERROR, backtraces
    idx, *out_vals = yield (main, *args), {}
    del main
  yield (idx, out_vals), backtraces

@lu.transformation
def nancheck_subtrace(main, *args):
  trace = NanTrace(main, core.cur_sublevel())
  in_tracers = [NanTracer(trace, x) for x in args]
  ans = yield in_tracers, {}
  out_tracers = map(trace.full_raise, ans)
  out_vals = [t.val for t in out_tracers]
  yield [main.idx, *out_vals]