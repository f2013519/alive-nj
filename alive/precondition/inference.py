from . import enumerator
from .. import language as L
from .. import typing
from .. import smtinterp
from ..analysis import safety
from ..util.pretty import pformat
from ..z3util import mk_and, mk_or
import collections
import itertools
import logging
import random
import z3

logger = logging.getLogger(__name__)

CONFLICT_SET_CUTOFF = 16

def mk_implies(premises, consequent):
  if premises:
    return z3.Implies(mk_and(premises), consequent)

  return consequent


TestCase = collections.namedtuple('TestCase', ['type_vector', 'values'])

def test_feature(pred, test_case, cache):
  try:
    pred_smt = cache[test_case.type_vector]
  except KeyError:
    smt = safety.Translator(test_case.type_vector)
    p,pd,pp,pq = smt(pred)
    assert not (pd or pp or pq)
    ps = smt.reset_safe()
    pred_smt = mk_and(ps + [p])
    cache[test_case.type_vector] = pred_smt

  e = z3.simplify(z3.substitute(pred_smt, *test_case.values))
  assert z3.is_bool(e)
  return z3.is_true(e)

# symbols, type_model just get passed through to the enumerator
def learn_feature(config, good, bad):
  log = logger.getChild('learn_feature')
  for size in itertools.count(3):
    log.info('Checking size %s', size)
    for pred in enumerator.predicates(size, config):
      log.debug('Checking %s', pred)

      cache = {}
      if all(test_feature(pred, g, cache) for g in good) and \
          all(not test_feature(pred, b, cache) for b in bad):
        return pred, cache

def find_largest_conflict_set(vectors):
  largest = 0
  chosen = None

  for _,g,b in vectors:
    if not g or not b: continue

    size = len(g) + len(b)
    if size > largest:
      largest = size
      chosen = (g,b)

  return chosen

def sample_largest_conflict_set(vectors):
  chosen = find_largest_conflict_set(vectors)

  if chosen:
    g,b = chosen
    if len(g) + len(b) > CONFLICT_SET_CUTOFF:
      x = random.randrange(
            max(1, CONFLICT_SET_CUTOFF - len(b)),
            min(CONFLICT_SET_CUTOFF, len(g)+1))

      g = random.sample(g, x)
      b = random.sample(b, CONFLICT_SET_CUTOFF - x)
      assert len(g) + len(b) == CONFLICT_SET_CUTOFF
      chosen = (g,b)

  return chosen

find_conflict_set = sample_largest_conflict_set

def partition(feature, cache, cases):
  sats = []
  unsats = []
  
  for tc in cases:
    if test_feature(feature, tc, cache):
      sats.append(tc)
    else:
      unsats.append(tc)

  return sats, unsats

def extend_feature_vectors(vectors, feature, cache=None):
  if cache is None:
    cache = {}

  new_vectors = []
  for vector, good, bad in vectors:
    good_t, good_f = partition(feature, cache, good)
    bad_t, bad_f = partition(feature, cache, bad)

    if good_t or bad_t:
      new_vectors.append((vector + (True,), good_t, bad_t))
    if good_f or bad_f:
      new_vectors.append((vector + (False,), good_f, bad_f))

  return new_vectors

def clause_accepts(clause, vector):
  return any((l < 0 and not vector[l]) or (l >= 0 and vector[l]) for l in clause)

def consistent_clause(clause, vectors):
  return all(clause_accepts(clause, v) for v in vectors)

def learn_boolean(feature_count, goods, bads):
  log = logger.getChild('learn_bool')
  log.debug('called with %s features; vectors: %s good, %s bad', feature_count,
    len(goods), len(bads))

  clauses = []
  excluded_by = [] # for each clause, the bad vector ids it excludes
  excluding = collections.defaultdict(set) # n -> set of clauses excluding n vectors
  excludes = collections.defaultdict(list) # vector id -> list of clauses

  lits = range(-feature_count, feature_count)
  k = 0
  
  # generate clauses until all bad vectors are excluded
  while len(excludes) < len(bads):
    k += 1
    clauses.extend(c for c in itertools.combinations(lits, k)
      if consistent_clause(c, goods))

    log.debug('size %s; %s consistent clauses', k, len(clauses))

    # note the vectors excluded by each new clause
    for c in xrange(len(excluded_by), len(clauses)):
      exc = set()
      for v,vector in enumerate(bads):
        if not clause_accepts(clauses[c], vector):
          exc.add(v)
          excludes[v].append(c)
      excluded_by.append(exc)
      excluding[len(exc)].add(c)

    log.debug('%s of %s bad vectors excluded', len(excludes), len(bads))

  cover = []

  # repeatedly select the clause which excludes the most bad vectors
  for s in xrange(max(excluding), 0, -1):
    if s not in excluding: continue

    cs = excluding[s]
    log.debug('%s vectors to exclude', len(excludes))

    while cs:
      log.debug('%s clauses excluding %s', len(cs), s)

      # select arbitrary clause
      # (better to select the smallest clause?)
      c = cs.pop()

      cover.append(clauses[c])

      # remove all vectors excluded by clauses[c]
      for v in excluded_by[c]:
        for xc in excludes.pop(v):
          if xc == c: continue
          
          #log.debug('deleting vector %s from clause %s', v, xc)
          exc = excluded_by[xc]
          excluding[len(exc)].remove(xc)
          exc.remove(v)
          excluding[len(exc)].add(xc)

  return cover

def mk_AndPred(clauses):
  clauses = tuple(clauses)
  if len(clauses) == 1:
    return clauses[0]
  
  return L.AndPred(*clauses)

def mk_OrPred(clauses):
  clauses = tuple(clauses)
  if len(clauses) == 1:
    return clauses[0]
  
  return L.OrPred(*clauses)


def infer_precondition_by_examples(config, goods, bads,
    features=None):
  """Synthesize a precondition which accepts the good examples and
  rejects the bad examples.
  
  features is None or an initial list of features; additional features
  will be appended to this list.
  """
  log = logger.getChild('pie')

  if features is None:
    features = []

  log.info('Inferring: %s good, %s bad, %s features', len(goods),
    len(bads), len(features))

  feature_vectors = [((),goods,bads)]
  for f in features:
    feature_vectors = extend_feature_vectors(feature_vectors, f)

    if log.isEnabledFor(logging.DEBUG):
      log.debug('Feature Vectors\n  ' +
        pformat([(v,len(g),len(b)) for (v,g,b) in feature_vectors],
          indent=2))

  while True:
    # find a conflict set
    conflict = find_conflict_set(feature_vectors)
    if conflict is None:
      break

    f, cache = learn_feature(config, conflict[0], conflict[1])
    log.info('Feature %s: %s', len(features), f)

    features.append(f)
    
    feature_vectors = extend_feature_vectors(feature_vectors, f, cache)

    if log.isEnabledFor(logging.DEBUG):
      log.debug('Feature Vectors\n  ' +
        pformat([(v,len(g),len(b)) for (v,g,b) in feature_vectors],
          indent=2))

  good_vectors = []
  bad_vectors = []
  
  for vector, g, b in feature_vectors:
    assert not g or not b
    if g:
      good_vectors.append(vector)
    else:
      bad_vectors.append(vector)

  clauses = learn_boolean(len(features), good_vectors, bad_vectors)

  pre = mk_AndPred(
          mk_OrPred(
            L.NotPred(features[l]) if l < 0 else features[l] for l in c)
          for c in clauses)

  return pre

def random_test_cases(num, expr, symbols, type_vector, filter=None):
  smt = smtinterp.SMTPoison(type_vector)

  symbol_types = [type_vector[typing.context[t]] for t in symbols]
  symbol_smts = [smt.eval(t) for t in symbols]

  goods = []
  bads = []

  for _ in xrange(num):
    tc_vals = (z3.BitVecVal(random.randrange(0, 2**ty.width), ty.width)
      for ty in symbol_types)

    tc = TestCase(type_vector, tuple(itertools.izip(symbol_smts, tc_vals)))

    if filter:
      s = z3.Solver()
      s.add(z3.substitute(filter, *tc.values))
      res = s.check()
      if res == z3.unsat:
        continue
      if res == z3.unknown:
        logging.warn('Unknown result for filter %s', tc)
        continue

    s = z3.Solver()
    s.add(z3.Not(z3.substitute(expr, *tc.values)))
    res = s.check()
    if res == z3.unsat:
      goods.append(tc)
    elif res == z3.sat:
      bads.append(tc)
    else:
      logging.warn('Unknown result for tc %s', tc)

  return goods, bads

def get_models(expr, vars):
  """Generate tuples satisfying the expression.
  """

  s = z3.Solver()
  s.add(expr)
  res = s.check()

  while res == z3.sat:
    model = s.model()
    yield tuple((v,model[v]) for v in vars)

    s.add(z3.Or([v != model[v] for v in vars]))
    res = s.check()

  if res == z3.unknown:
    raise Exception('Solver returned unknown: ' + s.reason_unknown())

def interpret_opt(smt, opt):
  """Translate opt to form mk_and(S + P) => Q and return S, P, Q.
  """
  
  sv,sd,sp,sq = smt(opt.src)
  if sq:
    raise Exception('quantified variables in opt {!r}'.format(opt.name))

  assert not smt.reset_safe()

  sd.extend(sp)

  tv,td,tp,_ = smt(opt.tgt)
  ts = smt.reset_safe()

  td.extend(tp)
  td.append(sv == tv)

  return ts, sd, mk_and(td)

def make_test_cases(opt, symbols, inputs, type_vectors, 
    num_random, num_good, num_bad):
  log = logger.getChild('make_test_cases')

  goods = []
  bads = []
  
  assert num_bad > 0
  num_random = max(0, num_random)

  for type_vector in type_vectors:
    smt = safety.Translator(type_vector)

    symbol_smts = [smt.eval(t) for t in symbols]

    safe, premises, consequent = interpret_opt(smt, opt)

    e = mk_implies(safe + premises, consequent)
    log.debug('Query: %s', e)

    solver_bads = list(
      itertools.islice(get_models(z3.Not(e), symbol_smts), num_bad))

    log.debug('%s counter-examples', len(solver_bads))

    bads.extend(TestCase(type_vector, tc) for tc in solver_bads)

    if num_good > 0:
      input_smts = [smt.eval(t) for t in inputs]
    
      query = mk_and(premises + [z3.ForAll(input_smts, e)])
      solver_goods = list(
        itertools.islice(get_models(query, symbol_smts), num_good))
      
      log.debug('%s pro-examples', len(solver_goods))
      
      goods.extend(TestCase(type_vector, tc) for tc in solver_goods)
  
    filter = mk_and(premises) if premises else None

    r_goods, r_bads = random_test_cases(num_random, e, symbols, type_vector, filter)
    
    log.debug('randoms: %s good, %s bad', len(r_goods), len(r_bads))
    
    goods.extend(r_goods)
    bads.extend(r_bads)

  return goods, bads


def infer_precondition(opt,
    features=None,
    random_cases=100,
    solver_good=10,
    solver_bad=10):
  log = logger.getChild('infer')

  if log.isEnabledFor(logging.INFO):
    log.info('infer_precondtion invoked on %r (%s features,'
      '%s randoms, %s +solver, %s -solver',
      opt.name, 'No' if features is None else len(features),
      random_cases, solver_good, solver_bad)

  type_model = opt.abstract_type_model()
  type_vectors = list(itertools.islice(type_model.type_vectors(), 4))
    # FIXME: configure number of inital vectors somehow

  symbols = []
  inputs = []
  reps = [None] * type_model.tyvars
  for t in L.subterms(opt.src):
    reps[typing.context[t]] = t
    if isinstance(t, L.Symbol):
      symbols.append(t)
    elif isinstance(t, L.Input):
      inputs.append(t)

  reps = [r for r in reps if r is not None]
  assert all(isinstance(t, L.Value) for t in reps)

  goods, bads = make_test_cases(opt, symbols, inputs, type_vectors,
    random_cases, solver_good, solver_bad)
  
  log.info('Initial test cases: %s good, %s bad', len(goods), len(bads))
  
  valid = not bads
  pre = None

  config = enumerator.Config(symbols, reps, type_model)

  while not valid:
    pre = infer_precondition_by_examples(config, goods, bads, features)

    if log.isEnabledFor(logging.INFO):
      log.info('Inferred precondition:\n  ' + pformat(pre, indent=2))

    valid = True

    for type_vector in type_vectors:
      smt = safety.Translator(type_vector)

      tgt_safe, premises, consequent = interpret_opt(smt, opt)  # cache this?

      log.debug('\ntgt_safe %s\npremises %s\nconsequent %s',
        tgt_safe, premises, consequent)

      pb,pd,_,_ = smt(pre)
      pre_safe = smt.reset_safe()
      pd.append(pb)

      log.debug('\npre_safe %s\npd %s', pre_safe, pd)

      if tgt_safe:
        pre_safe.append(mk_implies(pd, mk_and(tgt_safe)))

      premises.extend(pd)

      e = mk_implies(pre_safe + premises, consequent)
      log.debug('Validity check: %s', e)

      symbol_smts = [smt.eval(t) for t in symbols]
      counter_examples = list(itertools.islice(
        get_models(z3.Not(e), symbol_smts), solver_bad))

      log.info('counter-examples: %s', len(counter_examples))
      
      if counter_examples:
        valid = False
        bads.extend(TestCase(type_vector, tc) for tc in counter_examples)
        break

  return pre