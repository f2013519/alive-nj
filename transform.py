'''
General object representing transformations (optimizations).
'''

from language import *
from typing import TypeConstraints
from smtinterp import check_refinement_at
import logging

logger = logging.getLogger(__name__)


class Transform(object):
  def __init__(self, src, tgt, pre=None, name='', replacements=None):
    self.name = name
    self.pre = pre
    self.src = src
    self.tgt = tgt
    self.replace = tuple(replacements) if replacements else ()

  def z3_models(self):
    logger.debug('Gathering type constraints for ' + repr(self.name))
    t = TypeConstraints()
    t.eq_types(self.src, self.tgt)
    if self.pre:
      self.pre.accept(t)
    
    return t.z3_models()

  def check_refinement(self, model):
    # TODO: check all replacements

    for s,t in self.replace:
      logger.info('Checking intermediate refinement %r, %r', s, t)

      r = check_refinement_at(model, s, t, self.pre)
      if r:
        logger.info('Refinement check failed %r', r)
        return r

    logger.info('Checking refinement for root')
    r = check_refinement_at(model, self.src, self.tgt, self.pre)
    if r:
      logger.info('Check failed %r', r)
      return r

  def format(self):
    s = ''
    if self.name:
      s += 'Name: ' + self.name + '\n'
      
    f = Formatter()
    srci = get_insts(self.src)
    
    src_lines = [i.accept(f) for i in srci]
    
    if logger.isEnabledFor(logging.DEBUG):
      from pprint import pformat
      logger.debug('Generated source\n%s\n%s',
        '\n'.join(src_lines), pformat(f.ids))

    if self.pre:
      s += 'Pre: ' + self.pre.accept(f) + '\n'
    
    s += '\n'.join(src_lines) + '\n=>\n'
    
    for v,_ in self.replace:
      del f.ids[v]

    tgti = get_insts(self.tgt)
    s += '\n'.join(i.accept(f) for i in tgti if i not in f.ids)
    
    return s

def get_insts(v):
  def walk(v, insts, seen):
    if v in seen or not isinstance(v, Instruction):
      return
    
    seen.add(v)

    for a in v.args():
      walk(a, insts, seen)
    
    insts.append(v)

  seen = set()
  insts = []
  walk(v, insts, seen)
  return insts

class Formatter(Visitor):
  def __init__(self):
    self.ids = {}
    self.fresh = 0

  def name(self, term):
    if term in self.ids: return self.ids[term]

    prefix = '%'
    if isinstance(term, (Input, Instruction)) and term.name:
      name = term.name
      prefix = name[0]
    else:
      name = prefix + str(self.fresh)
      self.fresh += 1

    while name in self.ids:
      name = prefix + str(self.fresh)
      self.fresh += 1

    self.ids[term] = name
    return name

  def operand(self, term, ty = None):
    ty = self.ty(ty)      
    if ty: ty = ty + ' '

    if isinstance(term, Instruction):
      return ty + self.name(term)

    return ty + term.accept(self)

  def ty(self, ty):
    if ty is None:
      return ''
    if isinstance(ty, IntType):
      return 'i' + str(ty.width)

    assert False

  def Input(self, term):
    return self.name(term)

  def BinaryOperator(self, term):
    return self.name(term) + ' = ' + term.code + ' ' + \
      (' '.join(term.flags) + ' ' if term.flags else '') +\
      self.operand(term.x, term.ty) + ', ' + self.operand(term.y)

  def ConversionInst(self, term):
    return self.name(term) + ' = ' + term.code + ' ' + \
      self.operand(term.arg, term.ty) + \
      (' to ' + self.ty(term.src_ty) if term.src_ty else '')

  def IcmpInst(self, term):
    return 'icmp ' + term.pred + ' ' + \
      self.operand(term.x, term.ty) + ', ' + self.operand(term.y)

  def SelectInst(self, term):
    return 'select ' + self.operand(term.sel) + ', ' + \
      self.operand(term.arg1, term.ty1) + ', ' + \
      self.operand(term.arg2, term.ty2)

  def Literal(self, term):
    return str(term.val)

  def BinaryCnxp(self, term):
    return '(' + \
      ' '.join((self.operand(term.x), term.code, self.operand(term.y))) + \
      ')'

  def UnaryCnxp(self, term):
    return term.code + self.operand(term.x)

  def FunCnxp(self, term):
    return term.code + '(' + \
      ', '.join(self.operand(a) for a in term._args) + ')'


  def AndPred(self, term):
    if len(term.clauses) == 0:
      return 'true'

    return '(' + ' && '.join(cl.accept(self) for cl in term.clauses) + ')'

  def OrPred(self, term):
    if len(term.clauses) == 0:
      return '!true'

    return '(' + ' || '.join(cl.accept(self) for cl in term.clauses) + ')'

  def NotPred(self, term):
    return '!' + term.p.accept(self)

  def Comparison(self, term):
    code = {
      'eq':  '==',
      'ne':  '!=',
      'slt': '<',
      'sle': '<=',
      'sgt': '>',
      'sge': '>=',
      'ult': 'u<',
      'ule': 'u<=',
      'ugt': 'u>',
      'uge': 'u>=',
      }[term.op]
    
    return ' '.join((self.operand(term.x), code, self.operand(term.y)))

  def FunPred(self, term):
    return term.code + '(' + ', '.join(self.operand(a) for a in term._args) + ')'