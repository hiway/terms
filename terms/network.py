# Copyright (c) 2007-2012 by Enrique Pérez Arnaud <enriquepablo@gmail.com>
#
# This file is part of the terms project.
# https://github.com/enriquepablo/terms
#
# The terms project is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# The terms project is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with any part of the terms project.
# If not, see <http://www.gnu.org/licenses/>.

import re

from sqlalchemy import Table, Column, Sequence
from sqlalchemy import ForeignKey, Integer, String, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, backref, aliased
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.exc import OperationalError

from terms.words import get_name, get_type, get_bases
from terms.words import isa, exists, thing
from terms.terms import Base, Term, term_to_base
from terms.predicates import Predicate
from terms.lexicon import Lexicon
from terms.factset import FactSet
from terms import exceptions
from terms import patterns


class Match(dict):

    def __init__(self, fact, prem=None):
        self.fact = fact
        self.paths = []
        self.prem = prem
        super(Match, self).__init__()

    def copy(self):
        new_match = Match(self.fact)
        for k, v in self.items():
            new_match[k] = v
        new_match.prem = self.prem
        new_match.paths = self.paths[:]
        return new_match

    def merge(self, m):
        new_match = Match(self.fact)
        for k, v in self.items() + m.items():
            if k in m:
                if self[k] != v:
                    return False
            new_match[k] = v
        return new_match


class Network(object):

    def __init__(self, dbaddr='sqlite:///:memory:'):
        self.engine = create_engine(dbaddr)
        Session = sessionmaker(bind=self.engine)
        self.session = Session()
        self.lexicon = Lexicon(self.session)
        self.factset = FactSet(self.lexicon)
        try:
            self.root = self.session.query(RootNode).one()
        except OperationalError:
            self.initialize()

    def initialize(self):
        Base.metadata.create_all(self.engine)
        self.lexicon.initialize()
        self.factset.initialize()
        self.root = RootNode()
        self.session.add(self.root)
        self.session.commit()

    def _get_nclass(self, ntype):
        mapper = Node.__mapper__
        try:
            return mapper.base_mapper.polymorphic_map[ntype].class_
        except KeyError:
            return None

    def add_fact(self, fact):
        if self.root.child_path:
            m = Match(fact)
            m.paths = self.factset.get_paths(fact)
            ntype_name = self.root.child_path[-1]
            cls = self._get_nclass(ntype_name)
            cls.dispatch(self.root, m, fact, self)
        self.factset.add_fact(fact)

    def add_rule(self, prems, conds, cons, orders=None, _commit=True):
        rule = Rule()
        for prem in prems:
            vars = {}
            paths = self.factset.get_paths(prem)
            old_node = self.root
            for path in paths:
                node = self.get_or_create_node(old_node, prem, path, vars, rule)
                old_node = node
            if old_node.terminal:
                pnode = old_node.terminal
            else:
                pnode = PremNode(old_node)
                old_node.terminal = pnode
            pnode.rules.append(rule)
            for n, varname in vars.values():
                rule.pvars.append(PVarname(pnode, n, varname))
        rule.conds = conds
        for con in cons:
            rule.consecuences.append(con)
        if _commit:
            self.session.commit()

    def get_or_create_node(self, parent, term, path, vars, rule, _commit=False):
        ntype_name = path[-1]
        cls = self._get_nclass(ntype_name)
        value = cls.resolve(w, path)
        name = value.name
        m = patterns.varpat.match(name)
        pnum = 0
        if m:
            if name not in vars:
                pnum = len(vars)
                vars[name] = (pnum, Varname(int(m.group(3)), value, rule))
            else:
                pnum = vars[name][0]
        try:
            node = parent.children.join(cls, Node.id==cls.nid).filter(Node.var==pnum, cls.value==value).one()
        except NoResultFound:
            #  build the node and append it
            node = cls(value)
            node.var = pnum
            self.session.add(node)
            parent.children.append(node)
            if not parent.child_path:
                parent.child_path = path
            if _commit:
                self.session.commit()
        return node


class Node(Base):
    '''
    An abstact node in the primary (or premises) network.
    It is extended by concrete node classes.
    '''
    __tablename__ = 'nodes'
    id = Column(Integer, Sequence('node_id_seq'), primary_key=True)
    child_path_str = Column(String)
    var = Column(Integer, default=0)
    parent_id = Column(Integer, ForeignKey('nodes.id'))
    children = relationship('Node',
                         backref=backref('parent',
                                         uselist=False,
                                         remote_side=[id]),
                         primaryjoin="Node.id==Node.parent_id",
                         lazy='dynamic')

    ntype = Column(Integer)
    __mapper_args__ = {'polymorphic_on': ntype}

    def __init__(self, value):
        self.value = value

    def _get_path(self):
        try:
            return self._path
        except AttributeError:
            try:
                self._path = tuple(self.child_path_str.split('.'))
            except AttributeError:
                return ()
            return self._path

    def _set_path(self, path):
        self.child_path_str = '.'.join(path)
        self._path = path

    child_path = property(_get_path, _set_path)

    @classmethod
    def resolve(cls, w, path):
        '''
        Get the value pointed at by path in w (a word).
        It can be a boolean (for neg nodes),
        a sting (for label nodes),
        a word, or some custom value for custom node types.
        '''
        raise NotImplementedError

    @classmethod
    def dispatch(cls, parent, match, network):
        if parent.child_path:
            path = parent.child_path
            ntype_name = path[-1]
            cls = network._get_nclass(ntype_name)
            value = cls.resolve(match.fact, path)
            if value is None:
                children = parent.children.all()
            else:
                children = cls.get_children(parent, match, value, network)
            for ch in children:
                for child in ch:
                    new_match = match.copy()
                    if child.var:
                        if child.var not in match:
                            new_match[child.var] = value
                    cls.dispatch(child, new_match, network)
        if parent.terminal:
            parent.terminal.dispatch(match, network)

    @classmethod
    def get_children(cls, parent, match, value, factset):
        '''
        Get the value pointed at by path in w (a word).
        It can be a boolean (for neg nodes),
        a sting (for label nodes),
        a word, or some custom value for custom node types.
        '''
        raise NotImplementedError


class RootNode(Node):
    '''
    A root node
    '''
    __tablename__ = 'rootnodes'
    __mapper_args__ = {'polymorphic_identity': '_root'}
    nid = Column(Integer, ForeignKey('nodes.id'), primary_key=True)

    def __init__(self):
        pass


class NegNode(Node):
    '''
    A node that tests whether a predicate is negated
    '''
    __tablename__ = 'negnodes'
    __mapper_args__ = {'polymorphic_identity': '_neg'}

    nid = Column(Integer, ForeignKey('nodes.id'), primary_key=True)
    value = Column(Boolean)
    
    @classmethod
    def resolve(cls, term, path):
        try:
            for segment in path[:-1]:
                term = term.get_object(segment)
        except AttributeError:
            return None
        return term.true

    @classmethod
    def get_children(cls, parent, match, value, factset):
        return [parent.children.join(cls, Node.id==cls.nid).filter(cls.value==value)]


class TermNode(Node):
    '''
    '''
    __tablename__ = 'termnodes'
    __mapper_args__ = {'polymorphic_identity': '_term'}
    nid = Column(Integer, ForeignKey('nodes.id'), primary_key=True)
    term_id = Column(Integer, ForeignKey('terms.id'))
    value = relationship('Term',
                         primaryjoin="Term.id==TermNode.term_id")
    
    @classmethod
    def resolve(cls, term, path):
        '''
        Get the value pointed at by path in w (a word).
        It can be a boolean (for neg nodes),
        a sting (for label nodes),
        a word, or some custom value for custom node types.
        '''
        try:
            for segment in path[:-1]:
                term = term.get_object(segment)
        except AttributeError:
            return None
        return term

    @classmethod
    def get_children(cls, parent, match, value, network):
        children = parent.children.join(cls, Node.id==cls.nid).filter(cls.value==value)
        for k, v in match.items():
            if v == value:
                vchildren = parent.children.filter(Node.var==k)  # XXX poner la var al crear el nodo - si ya se ha usado
                break
        else:
            types = (value.term_type,) + get_bases(value.term_type)
            type_ids = [t.id for t in types]
            vchildren = parent.children.join(cls, Node.id==cls.nid).join(Term, cls.term_id==Term.id).filter(Term.var>0).filter(Term.type_id.in_(type_ids))
            if not isa(value, thing):
                bases = (value,) + get_bases(value)
                tbases = aliased(Term)
                base_ids = [b.id for b in bases]
                vchildren = vchildren.join(term_to_base, Term.id==term_to_base.c.term_id).join(tbases, term_to_base.c.base_id==tbases.id).filter(tbases.id.in_(base_ids))  # XXX can get duplicates
        return children, vchildren


class VerbNode(Node):
    '''
    '''
    __tablename__ = 'verbnodes'
    __mapper_args__ = {'polymorphic_identity': '_verb'}
    nid = Column(Integer, ForeignKey('nodes.id'), primary_key=True)
    term_id = Column(Integer, ForeignKey('terms.id'))
    value = relationship('Term',
                         primaryjoin="Term.id==VerbNode.term_id")
    
    @classmethod
    def resolve(cls, term, path):
        try:
            for segment in path[:-1]:
                term = term.get_object(segment)
        except AttributeError:
            return None
        if patterns.varpat.match(term.name):
            return w
        return term.term_type

    @classmethod
    def get_children(cls, parent, match, value, network):
        children = parent.children.join(cls, Node.id==cls.nid).filter(cls.value==value)
        pchildren = []
        for k,v in match.items():
            if v == value:
                vchildren = parent.children.filter(Node.var==k) # XXX falta poner las var
                break
        else:
            types = (value.term_type,) + get_bases(value.term_type)
            type_ids = [t.id for t in types]
            chvars = parent.children.join(cls, Node.id==cls.nid).join(Term, cls.term_id==Term.id).filter(Term.var>0)
            pchildren = chvars.filter(Term.type_id.in_(type_ids))
            tbases = aliased(Term)
            vchildren = chvars.join(term_to_base, Term.id==term_to_base.c.term_id).join(tbases, term_to_base.c.base_id==tbases.id).filter(tbases.id.in_(type_ids))
        return children, pchildren, vchildren


class LabelNode(Node):
    '''
    '''
    __tablename__ = 'labelnodes'
    __mapper_args__ = {'polymorphic_identity': '_label'}
    nid = Column(Integer, ForeignKey('nodes.id'), primary_key=True)
    value = Column(String)

    @classmethod
    def resolve(cls, w, path):
        return path[-2]

    @classmethod
    def get_children(cls, parent, match, value, factset):
        return [parent.children.all()]


prem_to_rule = Table('prem_to_rule', Base.metadata,
    Column('prem_id', Integer, ForeignKey('premnodes.id'), primary_key=True),
    Column('rule_id', Integer, ForeignKey('rules.id'), primary_key=True)
)


class PremNode(Base):
    '''
    a terminal node for a premise
    '''
    __tablename__ = 'premnodes'

    id = Column(Integer, Sequence('premnode_id_seq'), primary_key=True)
    parent_id = Column(Integer, ForeignKey('nodes.id'))
    parent = relationship('Node', backref=backref('terminal', uselist=False),
                         primaryjoin="Node.id==PremNode.parent_id")
    rules = relationship('Rule', backref='prems', secondary=prem_to_rule)

    def __init__(self, parent):
        self.parent = parent  # node

    def dispatch(self, match, network):
        mps = []
        m = Match()
        for var in self.vars:
            m.mpairs.append(MPair(var=var, val=match(var.name)))
        self.matches.append(m)
        for rule in self.rules:
            matches = [match]
            for prem in rule.prems:
                if prem is not self:
                    for m in matches
                        new_matches = []
                        pmatches = prem.matches
                        for v in m:
                            if v in prem.vars
                                a = aliased(MPair)
                                pmatches = pmatches.join(a, Match.id==a.match_id).filter(a.var==v, a.val==m[v.name])
                        for pm in pmatches:
                            new_match = m.copy()
                            for mpair in pm.mpairs:
                                vname = mpair.var.name
                                if vname not in m:
                                    new_match[vname] = mpair.val
                            new_matches.append(new_match)
                    matches = new_matches
            for m in matches:
                rule.dispatch(m)  # test the conditions, add the consecuences


## POR AQUI XXX
# faltan dos cosas.
# 1- arreglar las reglas, quitar los mnodes y poner premisas con (muchos) objetos match que tienen cada uno tantas parejas var - value como vars aparezcan en la prem. cuando una frase cuadra en una prem, la prem dispatch: por cada regla a la que pertenece, selecciona los matches del  resto de las premisas de la regla que cuadran con el suyo, y los añade como consecuencias.
# 2- definitivamente quitar words, poner predicates en el módulo de terms, manejar terms en el compiler. los predicate solo se guardan como consecuencias. no puse terms porque no podían ser variables, y ahora pueden. consecuence es más código que que predicate y más inútil y redundante. En el compiler se construyen varnames.


class Varname(Base):
    """
    a variable in a rule,
    it has a name
    """
    __tablename__ = 'varnames'

    id = Column(Integer, Sequence('varname_id_seq'), primary_key=True)
    name_num = Column(Integer)
    rule_id = Column(Integer, ForeignKey('rules.id'))
    rule = relationship('Rule', backref='varnames',
                         primaryjoin="Rule.id==Varname.rule_id")
    term_id = Column(Integer, ForeignKey('terms.id'))
    var = relationship('Term', backref='varnames',
                         primaryjoin="Term.id==Varname.term_id")

    def __init__(self, name_num, var, rule):
        self.name_num = name_num
        self.var = var
        self.rule = rule

    def _get_name(self):
        return self.var.name + str(self.name_num)

    name = property(_get_name)


class MNode(Base):
    '''
    '''
    __tablename__ = 'mnodes'


    id = Column(Integer, Sequence('mnode_id_seq'), primary_key=True)
    parent_id = Column(Integer, ForeignKey('mnodes.id'))
    parent = relationship('MNode', remote_side=[id], backref='children',
                         primaryjoin="MNode.id==MNode.parent_id")
    rule_id = Column(Integer, ForeignKey('rules.id'))
    rule = relationship('Rule', backref='mnodes',
                         primaryjoin="Rule.id==MNode.rule_id")
    prule_id = Column(Integer, ForeignKey('rules.id'))
    prule = relationship('Rule', backref=backref('mroot', uselist=False),
                         primaryjoin="Rule.id==MNode.prule_id")
    term_id = Column(Integer, ForeignKey('terms.id'))
    value = relationship('Term', backref='mnodes',
                         primaryjoin="Term.id==MNode.term_id")
    varname_id = Column(Integer, ForeignKey('varnames.id'))
    var = relationship('Varname', backref='mnodes',
                         primaryjoin="Varname.id==MNode.varname_id")
    fact_id = Column(Integer, ForeignKey('facts.id'))
    support = relationship('Fact', backref='mnodes',
                         primaryjoin="Fact.id==MNode.fact_id")

    def __init__(self, var, value, rule):
        self.var = var  # varname
        self.value = value  # term
        self.rule = rule
        # self.chidren = []  # mnodes
        # self.support = []  # facts that have supported it

    def dispatch(self, match, network, matched=None):
        """
        returns
         * None : mismatch
         * [] : no matches
         * [m1, m2...] : matches
        """

        if matched is None:
            matched = []

        new_matches = []
        first = self.children.first()
        var = first.var
        if var.name in match:
            matching = self.filter_value(match[var])
        else:
            matching = []

        if not matching: 
            new_match = match.copy()
            new_match[self.var] = self.value
            if len(new_match) == len(self.rule.vrs):
                return [new_match]
            else:
                new_matched = matched[:]
                new_matched.append(self.var)
                self.add_mnodes(new_match, new_matched)
                return []  # XXX

        for child in matching:
            new_matches.append(child.dispatch(match, network, matched))
        return [m for matches in new_matches for m in matches]

    def add_mnodes(self, match, matched, hint=None):
        if not hint:
            left = filter(lambda x: x not in matched, match.keys())
            for h in left:
                hint = h
                break
            else:
                return
        varname = self.rule.get_varname(hint)
        mnode = MNode(varname, match[hint], self.rule)
        self.children.append(mnode)
        matched.append(hint)
        mnode.add_mnodes(match, matched)

    def filter_value(self, val):
        bases = get_bases(val)
        return self.children.filter(MNode.value.in_(bases))



class PVarname(Base):
    """
    Mapping from varnames in rules (pvars belong in rules)
    to premise, number.
    Premises have numbered variables;
    and different rules can share a premise,
    but translate differently its numbrered vars to varnames.
    """
    __tablename__ = 'pvarnames'


    id = Column(Integer, Sequence('mnode_id_seq'), primary_key=True)
    rule_id = Column(Integer, ForeignKey('rules.id'))
    rule = relationship('Rule', backref=backref('pvars', lazy='dynamic'),
                         primaryjoin="Rule.id==PVarname.rule_id")
    prem_id = Column(Integer, ForeignKey('premnodes.id'))
    prem = relationship('PremNode', backref='pvars',
                         primaryjoin="PremNode.id==PVarname.prem_id")
    varname_id = Column(Integer, ForeignKey('varnames.id'))
    varname = relationship('Varname', backref='pvarnames',
                         primaryjoin="Varname.id==PVarname.varname_id")
    num = Column(Integer)

    def __init__(self, prem, num, varname):
        self.prem = prem
        self.num = num
        self.varname = varname


class CondArg(Base):
    '''
    '''
    __tablename__ = 'condargs'

    id = Column(Integer, Sequence('condarg_id_seq'), primary_key=True)
    cond_id = Column(Integer, ForeignKey('conditions.id'))
    cond = relationship('Condition', backref='args',
                         primaryjoin="Condition.id==CondArg.cond_id")
    varname_id = Column(Integer, ForeignKey('varnames.id'))
    varname = relationship('Varname', backref='condargs',
                         primaryjoin="Varname.id==CondArg.varname_id")
    term_id = Column(Integer, ForeignKey('terms.id'))
    term = relationship('Term',
                         primaryjoin="Term.id==CondArg.term_id")

    def __init__(self, val, ):
        if isinstance(val, Term):
            self.term = val
        elif isinstance(val, Varname):
            self.varname = val

    def solve(self, match):
        if self.var:
            return match[self.var.name]
        return self.term


class Condition(Base):
    '''
    '''
    __tablename__ = 'conditions'

    id = Column(Integer, Sequence('condition_id_seq'), primary_key=True)
    rule_id = Column(Integer, ForeignKey('rules.id'))
    rule = relationship('Rule', backref='conditions',
                         primaryjoin="Rule.id==Condition.rule_id")
    fpath = Column(String)

    def __init__(self, rule, fpath, *args):
        self.rule = rule
        self.fun = fresolve(fpath)  # callable
        self.fpath = fpath  # string
        for arg in args:
            self.args.append(arg)  # Arg. terms have conversors for different funs

    def test(self, match):
        sargs = []
        for arg in args:
            sargs.append(arg.solve(match))
        return self.fun(*sargs)


class Rule(Base):
    '''
    '''
    __tablename__ = 'rules'

    id = Column(Integer, Sequence('rule_id_seq'), primary_key=True)

    def __init__(self):
        self.mroot = MNode(None, None, self)  # empty mnode

    def _get_vname(self, name, d, vns):
        try:
            return getattr(self, d)[name]
        except AttributeError:
            setattr(self, d, {})
            target = None
            for vn in getattr(self, vns):
                n = vn.name
                getattr(self, d)[n] = vn
                if n == name:
                    target = vn
            return target

    def get_varname(self, name):
        return self._get_vname(name, '_vn_dict', 'varnames')

    def get_pvarname(self, name):
        return self._get_vname(name, '_pvn_dict', 'pvars')

    def dispatch(self, match, network):
        new_match = Match(match.fact)
        for num, o in match.items():
            pvar = self.get_pvarname((match.prem, num))
            varname = pvar.varname.name
            new_match[varname] = o

        for cond in self.conditions:
            if not cond.test(match):
                return

        for con in self.consecuences:
            network.add_fact(con.substitute(match))
        return new or False


