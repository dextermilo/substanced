import random

from persistent import Persistent

import BTrees

from zope.interface import implementer

from pyramid.compat import is_nonstr_iter
from pyramid.location import lineage
from pyramid.traversal import (
    resource_path_tuple,
    find_resource,
    )

from ..event import (
    subscribe_will_be_added,
    subscribe_removed,
    )
from ..util import (
    postorder,
    oid_of,
    acquire,
    )

from ..interfaces import IObjectMap

"""
Pathindex data structure of object map:

{pathtuple:{level:set_of_objectids, ...}, ...}

>>> map = ObjectMap()

If a object map with an otherwise empty pathindex has ``add('/a/b/c')``
called on it, and the objectid for ``/a/b/c`` winds up being ``1``, the path
index will end up looking like this:

>>> map.add('/a/b/c')
>>> map.pathindex

{(u'',):                  {3: set([1])},
 (u'', u'a'):             {2: set([1])},
 (u'', u'a', u'b'):       {1: set([1])},
 (u'', u'a', u'b', u'c'): {0: set([1])}}

(Level 0 is "this path")

If we then ``add('/a')`` and the result winds up as objectid 2, the pathindex
will look like this:

>>> map.add('/a')
>>> map.pathindex

{(u'',):                  {1: set([2]), 3: set([1])},
 (u'', u'a'):             {0: set([2]), 2: set([1])},
 (u'', u'a', u'b'):       {1: set([1])},
 (u'', u'a', u'b', u'c'): {0: set([1])}}

If we then add '/z' (and its objectid is 3):

>>> map.add('/z')
>>> map.pathindex

{(u'',):                  {1: set([2, 3]), 3: set([1])},
 (u'', u'a'):             {0: set([2]), 2: set([1])},
 (u'', u'a', u'b'):       {1: set([1])},
 (u'', u'a', u'b', u'c'): {0: set([1])},
 (u'', u'z'):             {0: set([3])}}

And so on and so forth as more items are added.  It is an error to attempt to
add an item to object map with a path that already exists in the object
map.

If '/a' (objectid 2) is then removed, the pathindex is adjusted to remove
references to the objectid represented by '/a' *and* any children
(in this case, there's a child at '/a/b/c').

>>> map.remove(2)
>>> map.pathindex

{(u'',):      {1: set([3])},
 (u'', u'z'): {0: set([3])}}
 
"""

_marker = object()

@implementer(IObjectMap)
class ObjectMap(Persistent):
    
    _v_nextid = None
    _randrange = random.randrange

    family = BTrees.family64

    def __init__(self, root, family=None):
        if family is not None:
            self.family = family
        self.objectid_to_path = self.family.IO.BTree()
        self.path_to_objectid = self.family.OI.BTree()
        self.pathindex = self.family.OO.BTree()
        self.referencemap = ReferenceMap()
        self.root = root

    def new_objectid(self):
        """ Obtain an unused integer object identifier """
        while True:
            if self._v_nextid is None:
                self._v_nextid = self._randrange(self.family.minint, 
                                                 self.family.maxint)

            objectid = self._v_nextid

            if objectid > self.family.maxint:
                self._v_nextid = None
                continue
                
            self._v_nextid += 1

            # object id zero is reserved as "irresolveable"
            if objectid != 0 and not objectid in self.objectid_to_path:
                return objectid

            self._v_nextid = None

    def objectid_for(self, obj_or_path_tuple):
        """ Returns an objectid or ``None``, given an object or a path tuple"""
        if isinstance(obj_or_path_tuple, tuple):
            path_tuple = obj_or_path_tuple
        elif hasattr(obj_or_path_tuple, '__parent__'):
            path_tuple = resource_path_tuple(obj_or_path_tuple)
        else:
            raise ValueError(
                'objectid_for accepts a traversable object or a path tuple, '
                'got %s' % (obj_or_path_tuple,))
        return self.path_to_objectid.get(path_tuple)

    def path_for(self, objectid):
        """ Returns an path or ``None`` given an object id """
        return self.objectid_to_path.get(objectid)

    def object_for(self, objectid_or_path_tuple, context=None):
        """ Returns an object or ``None`` given an object id or a path tuple"""
        if isinstance(objectid_or_path_tuple, (int, long)):
            path_tuple = self.objectid_to_path.get(objectid_or_path_tuple)
        elif isinstance(objectid_or_path_tuple, tuple):
            path_tuple = objectid_or_path_tuple
        else:
            raise ValueError('Unknown input %s' % (objectid_or_path_tuple,))
        try:
            return self._find_resource(context, path_tuple)
        except KeyError:
            return None

    def _find_resource(self, context, path_tuple): # replaced in tests
        if context is None:
            context = self.root
        return find_resource(context, path_tuple)
            
    def add(self, obj, path_tuple, replace_oid=False):
        """ Add a new object to the object map at the location specified by
        ``path_tuple`` (must be the path of the object in the object graph as
        a tuple, as returned by Pyramid's ``resource_path_tuple`` function)."""
        if not isinstance(path_tuple, tuple):
            raise ValueError('path_tuple argument must be a tuple')

        objectid = oid_of(obj, _marker)

        if objectid is _marker or replace_oid:
            objectid = self.new_objectid()
            obj.__objectid__ = objectid
        elif objectid in self.objectid_to_path:
            raise ValueError('objectid %s already exists' % (objectid,))

        if path_tuple in self.path_to_objectid:
            raise ValueError('path %s already exists' % (path_tuple,))

        self.path_to_objectid[path_tuple] = objectid
        self.objectid_to_path[objectid] = path_tuple

        pathlen = len(path_tuple)

        for x in range(pathlen):
            els = path_tuple[:x+1]
            omap = self.pathindex.setdefault(els, self.family.IO.BTree())
            level = pathlen - len(els)
            oidset = omap.setdefault(level, self.family.IF.Set())
            oidset.add(objectid)

        return objectid

    def remove(self, obj_objectid_or_path_tuple, references=True):
        """ Remove an object from the object map give an object, an object id
        or a path tuple.  If ``references`` is True, also remove any
        references added via ``connect``, otherwise leave them there
        (e.g. when moving an object)."""
        if hasattr(obj_objectid_or_path_tuple, '__parent__'):
            path_tuple = resource_path_tuple(obj_objectid_or_path_tuple)
        elif isinstance(obj_objectid_or_path_tuple, (int, long)):
            path_tuple = self.objectid_to_path[obj_objectid_or_path_tuple]
        elif isinstance(obj_objectid_or_path_tuple, tuple):
            path_tuple = obj_objectid_or_path_tuple
        else:
            raise ValueError(
                'Value passed to remove must be a traversable '
                'object, an object id, or a path tuple, got %s' % (
                    (obj_objectid_or_path_tuple,)))

        pathlen = len(path_tuple)

        omap = self.pathindex.get(path_tuple)

        # rationale: if this key isn't present, no path added ever contained it
        if omap is None:
            return set()

        removed = set()
        items = omap.items()
        removepaths = []
        
        for k, dm in self.pathindex.items(min=path_tuple):
            if k[:pathlen] == path_tuple:
                for oidset in dm.values():
                    removed.update(oidset)
                    for oid in oidset:
                        if oid in self.objectid_to_path:
                            p = self.objectid_to_path[oid]
                            del self.objectid_to_path[oid]
                            del self.path_to_objectid[p]
                # dont mutate while iterating
                removepaths.append(k)
            else:
                break

        for k in removepaths:
            del self.pathindex[k]

        for x in range(pathlen-1):

            offset = x + 1
            els = path_tuple[:pathlen-offset]
            omap2 = self.pathindex[els]
            for level, oidset in items:

                i = level + offset
                oidset2 = omap2[i]

                for oid in oidset:
                    if oid in oidset2:
                        oidset2.remove(oid)
                        # adding to removed and removing from objectid_to_path
                        # and path_to_objectid should have been taken care of
                        # above in the for k, dm in self.pathindex.items()
                        # loop
                        assert oid in removed, oid
                        assert not oid in self.objectid_to_path, oid

                if not oidset2:
                    del omap2[i]

        if references:
            self.referencemap.remove(removed)

        return removed

    def _get_path_tuple(self, obj_or_path_tuple):
        if hasattr(obj_or_path_tuple, '__parent__'):
            path_tuple = resource_path_tuple(obj_or_path_tuple)
        elif isinstance(obj_or_path_tuple, tuple):
            path_tuple = obj_or_path_tuple
        else:
            raise ValueError(
                'must provide a traversable object or a '
                'path tuple, got %s' % (obj_or_path_tuple,))
        return path_tuple
    
    def navgen(self, obj_or_path_tuple, depth=1):
        path_tuple = self._get_path_tuple(obj_or_path_tuple)
        return self._navgen(path_tuple, depth)

    def _navgen(self, path_tuple, depth):
        omap = self.pathindex.get(path_tuple)
        if omap is None:
            return []
        oidset = omap.get(1)
        result = []
        if oidset is None:
            return result
        newdepth = depth-1
        if newdepth > -1:
            for oid in oidset:
                pt = self.objectid_to_path[oid]
                result.append(
                    {'path':pt,
                     'children':self._navgen(pt, newdepth),
                     'name':pt[-1],
                     }
                    )
        return result

    def pathlookup(self, obj_or_path_tuple, depth=None, include_origin=True):
        """ Return a set of objectids under a given path given an object or a
        path tuple.  If ``depth`` is None, return all object ids under the
        path.  If ``depth`` is an integer, use that depth instead.  If
        ``include_origin`` is ``True``, include the object identifier of the
        object that was passed, otherwise omit it from the returned set."""
        path_tuple = self._get_path_tuple(obj_or_path_tuple)
        omap = self.pathindex.get(path_tuple)

        result = self.family.IF.Set()

        if omap is None:
            return result
        
        if depth is None:
            for d, oidset in omap.items():
                
                if d == 0 and not include_origin:
                    continue

                result.update(oidset)

        else:
            for d in range(depth+1):

                if d == 0 and not include_origin:
                    continue

                oidset = omap.get(d)

                if oidset is None:
                    continue
                else:
                    result.update(oidset)

        return result

    def _refids_for(self, source, target):
        sourceid, targetid = oid_of(source, source), oid_of(target, target)
        if not sourceid in self.objectid_to_path:
            raise ValueError('source %s is not in objectmap' % (source,))
        if not targetid in self.objectid_to_path:
            raise ValueError('target %s is not in objectmap' % (target,))
        return sourceid, targetid

    def _refid_for(self, obj):
        oid = oid_of(obj, obj)
        if not oid in self.objectid_to_path:
            raise ValueError('oid %s is not in objectmap' % (obj,))
        return oid

    def connect(self, source, target, reftype):
        """ Connect a source object or objectid to a target object or
        objectid using reference type ``reftype``"""
        sourceid, targetid = self._refids_for(source, target)
        self.referencemap.connect(sourceid, targetid, reftype)

    def disconnect(self, source, target, reftype):
        """ Disconnect a source object or objectid from a target object or
        objectid using reference type ``reftype``"""
        sourceid, targetid = self._refids_for(source, target)
        self.referencemap.disconnect(sourceid, targetid, reftype)

    # We make a copy of the set returned by ``targetids`` and ``sourceids``
    # because it's not atypical for callers to want to modify the
    # underlying bucket while iterating over the returned set.  For example:
    #
    # groups = objectmap.targetids(self, UserToGroup)
    # for group in groups:
    #    objectmap.disconnect(self, group, UserToGroup)
    #
    # if we don't make a copy, this kind of code will result in e.g.
    #
    #     for group in groups:
    # RuntimeError: the bucket being iterated changed size
    
    def sourceids(self, obj, reftype):
        """ Return a set of object identifiers of the objects connected to
        ``obj`` a a source using reference type ``reftype``"""
        oid = self._refid_for(obj)
        return self.family.IF.Set(self.referencemap.sourceids(oid, reftype))

    def targetids(self, obj, reftype):
        """ Return a set of object identifiers of the objects connected to
        ``obj`` a a target using reference type ``reftype``"""
        oid = self._refid_for(obj)
        return self.family.IF.Set(self.referencemap.targetids(oid, reftype))

    def sources(self, obj, reftype):
        """ Return a generator which will return the objects connected to
        ``obj`` as a source using reference type ``reftype``"""
        for oid in self.sourceids(obj, reftype):
            yield self.object_for(oid)

    def targets(self, obj, reftype):
        """ Return a generator which will return the objects connected to
        ``obj`` as a target using reference type ``reftype``"""
        for oid in self.targetids(obj, reftype):
            yield self.object_for(oid)

class ReferenceMap(Persistent):
    
    family = BTrees.family64
    
    def __init__(self, refmap=None):
        if refmap is None:
            refmap = self.family.OO.BTree()
        self.refmap = refmap

    def connect(self, source, target, reftype):
        refset = self.refmap.setdefault(reftype, ReferenceSet())
        refset.connect(source, target)

    def disconnect(self, source, target, reftype):
        refset = self.refmap.get(reftype)
        if refset is not None:
            refset.disconnect(source, target)

    def targetids(self, oid, reftype):
        refset = self.refmap.get(reftype)
        if refset is not None:
            return refset.targetids(oid)
        return self.family.IF.Set()

    def sourceids(self, oid, reftype):
        refset = self.refmap.get(reftype)
        if refset is not None:
            return refset.sourceids(oid)
        return self.family.IF.Set()

    def remove(self, oids):
        for refset in self.refmap.values():
            refset.remove(oids)

class ReferenceSet(Persistent):
    
    family = BTrees.family64

    def __init__(self):
        self.src2target = self.family.IO.BTree()
        self.target2src = self.family.IO.BTree()

    def connect(self, source, target):
        targets = self.src2target.setdefault(source, self.family.IF.TreeSet())
        targets.insert(target)
        sources = self.target2src.setdefault(target, self.family.IF.TreeSet())
        sources.insert(source)

    def disconnect(self, source, target):
        targets = self.src2target.get(source)
        if targets is not None:
            try:
                targets.remove(target)
            except KeyError:
                pass
            
        sources = self.target2src.get(target)
        if sources is not None:
            try:
                sources.remove(source)
            except KeyError:
                pass

    def targetids(self, oid):
        return self.src2target.get(oid, self.family.IF.Set())

    def sourceids(self, oid):
        return self.target2src.get(oid, self.family.IF.Set())

    def remove(self, oidset):
        # XXX is there a way to make this less expensive?
        removed = self.family.IF.Set()
        for oid in oidset:
            if oid in self.src2target:
                removed.insert(oid)
                targets = self.src2target.pop(oid)
                for target in targets:
                    oidset = self.target2src.get(target)
                    oidset.remove(oid)
                    if not oidset:
                        del self.target2src[target]
            if oid in self.target2src:
                removed.insert(oid)
                sources = self.target2src.pop(oid)
                for source in sources:
                    oidset = self.src2target.get(source)
                    oidset.remove(oid)
                    if not oidset:
                        del self.src2target[source]
        return removed
    
def node_path_tuple(resource):
    # cant use resource_path_tuple from pyramid, it wants everything to 
    # have a __name__
    return tuple(reversed([getattr(loc, '__name__', '') for 
                           loc in lineage(resource)]))

@subscribe_will_be_added()
def object_will_be_added(event):
    """ Objects added to folders must always have an __objectid__.  This must
     be an :class:`substanced.event.ObjectWillBeAdded` event subscriber
     so that a resulting object will have an __objectid__ within the (more
     convenient) :class:`substanced.event.ObjectAdded` fired later."""
    obj = event.object
    parent = event.parent
    objectmap = find_objectmap(parent)
    if objectmap is None:
        return
    if getattr(obj, '__parent__', None):
        raise ValueError(
            'obj %s added to folder %s already has a __parent__ attribute, '
            'please remove it completely from its existing parent (%s) before '
            'trying to readd it to this one' % (obj, parent, obj.__parent__)
            )
    basepath = resource_path_tuple(event.parent)
    name = event.name
    for node in postorder(obj):
        node_path = node_path_tuple(node)
        path_tuple = basepath + (name,) + node_path[1:]
        # the below gives node an objectid; if the will-be-added event is
        # the result of a duplication, replace the oid of the node with a new
        # one
        objectmap.add(node, path_tuple, replace_oid=event.duplicating)

@subscribe_removed()
def object_removed(event):
    """ :class:`substanced.event.ObjectRemoved` event subscriber.
    """
    obj = event.object
    parent = event.parent
    moving = event.moving
    objectmap = find_objectmap(parent)
    if objectmap is None:
        return
    objectid = oid_of(obj)
    objectmap.remove(objectid, references=not moving)

def _reference_property(reftype, resolve, orientation='source'):
    def _get(self, resolve=resolve):
        objectmap = find_objectmap(self)
        if orientation == 'source':
            oids = list(objectmap.targetids(self, reftype))
        else:
            oids = list(objectmap.sourceids(self, reftype))
        if not oids:
            oid = None
        else:
            assert(len(oids)==1)
            oid = oids[0]
        if resolve:
            return objectmap.object_for(oid)
        return oid

    def _set(self, oid):
        _del(self)
        if oid is None:
            return
        objectmap = find_objectmap(self)
        if orientation == 'source':
            objectmap.connect(self, oid, reftype)
        else:
            objectmap.connect(oid, self, reftype)

    def _del(self):
        oid = _get(self, resolve=False)
        if oid is None:
            return
        objectmap = find_objectmap(self)
        if orientation == 'source':
            objectmap.disconnect(self, oid, reftype)
        else:
            objectmap.disconnect(oid, self, reftype)

    return property(_get, _set, _del)

def reference_sourceid_property(reftype):
    """
    Returns a property which, when set, establishes an :term:`object map
    reference` between the property's instance (the source) and another
    object in the objectmap (the target) based on the reference type
    ``reftype``.  It is comparable to a Python 'weakref' between the
    persistent object instance which the property is attached to and the
    persistent target object id; when the target object or the object upon
    which the property is defined is removed from the system, the reference
    is destroyed.

    The ``reftype`` argument is a :term:`reference type`, a hashable object
    that describes the type of the relation.  See
    :meth:`substanced.objectmap.ObjectMap.connect` for more information about
    reference types.

    You can set, get, and delete the value.  When you set the value, a
    relation is formed between the object which houses the property and the
    target object id.  When you get the value, the related value (or ``None``
    if no relation exists) is returned, when you delete the value, the
    relation is destroyed and the value will revert to ``None``.

    For example:

    .. code-block:: python
       :linenos:

       # definition

       from substanced.content import content
       from substanced.objectmap import reference_sourceid_property

       @content('Profile')
       class Profile(Persistent):
           user_id = reference_sourceid_property('profile-to-userid')

       # subsequent usage of the property in a view...

       profile = registry.content.create('Profile')
       somefolder['profile'] = profile
       profile.user_id = oid_of(request.user)
       print profile.user_id # will print the oid of the user

       # if the user is later deleted by unrelated code...

       print profile.user_id # will print None

       # or if you delete the value explicitly...

       del profile.user_id
       print profile.user_id # will print None

    """
    return _reference_property(reftype, resolve=False)

def reference_targetid_property(reftype):
    """ Same as :func:`substanced.objectmap.reference_sourceid_property`,
    except the object upon which the property is defined is the *source* of
    the reference and any object assigned to the property is the target."""
    return _reference_property(reftype, resolve=False, orientation='target')

def reference_source_property(reftype):
    """
    Exactly like :func:`substanced.objectmap.reference_sourceid_property`,
    except its getter returns the *instance* related to the target instead of
    the target object id.  Likewise, its setter will accept another
    persistent object instance that has an object id.

    For example:

    .. code-block:: python
       :linenos:

       # definition

       from substanced.content import content
       from substanced.objectmap import reference_property

       @content('Profile')
       class Profile(Persistent):
           user = reference_property('profile-to-user')

       # subsequent usage of the property in a view...

       profile = registry.content.create('Profile')
       somefolder['profile'] = profile
       profile.user = request.user
       print profile.user # will print the user object

       # if the user is later deleted by unrelated code...

       print profile.user # will print None

       # or if you delete the value explicitly...

       del profile.user
       print profile.user # will print None
    
    """
    return _reference_property(reftype, resolve=True)

def reference_target_property(reftype):
    """ Same as :func:`substanced.objectmap.reference_source_property`,
    except the object upon which the property is defined is the *source* of
    the reference and any object assigned to the property is the target."""
    return _reference_property(reftype, resolve=True, orientation='target')

def _multireference_property(
    reftype,
    ignore_missing,
    resolve,
    orientation,
    ):

    def _get(self, resolve=resolve):
        objectmap = find_objectmap(self)
        if orientation == 'source':
            oids = objectmap.targetids(self, reftype)
        else:
            oids = objectmap.sourceids(self, reftype)
        return Multireference(
            self,
            oids,
            objectmap,
            reftype,
            ignore_missing,
            resolve,
            orientation,
            )

    def _set(self, oids):
        if not is_nonstr_iter(oids):
            raise ValueError('Must be a sequence')
        iterable = _del(self)
        iterable.connect(oids)

    def _del(self):
        iterable = _get(self, resolve=False)
        iterable.clear()
        return iterable

    return property(_get, _set, _del)

def multireference_sourceid_property(reftype, ignore_missing=False):
    """ Like :func:`substanced.objectmap.reference_sourceid_property`, but
    maintains a :class:`substanced.objectmap.Multireference` rather than an
    object id."""
    return _multireference_property(
        reftype,
        ignore_missing=ignore_missing,
        resolve=False,
        orientation='source'
        )

def multireference_source_property(reftype, ignore_missing=False):
    """ Like :func:`substanced.objectmap.reference_source_property`, but
    maintains a :class:`substanced.objectmap.Multireference` rather than a
    single object reference."""
    return _multireference_property(
        reftype,
        ignore_missing=ignore_missing,
        resolve=True,
        orientation='source'
        )

def multireference_targetid_property(reftype, ignore_missing=False):
    """ Like :func:`substanced.objectmap.reference_targetid_property`, but
    maintains a :class:`substanced.objectmap.Multireference` rather than an
    object id."""
    return _multireference_property(
        reftype,
        ignore_missing=ignore_missing,
        resolve=False,
        orientation='target'
        )

def multireference_target_property(reftype, ignore_missing=False):
    """ Like :func:`substanced.objectmap.reference_target_property`, but
    maintains a :class:`substanced.objectmap.Multireference` rather than a
    single object reference."""
    return _multireference_property(
        reftype,
        ignore_missing=ignore_missing,
        resolve=True,
        orientation='target'
        )

class Multireference(object):
    """ An iterable of objects (if ``resolve`` is true) or oids (if
    ``resolve`` is false).  Also supports the Python sequence protocol.

    Additionally supports ``connect``, ``disconnect``, and ``clear`` methods
    for mutating the relationships implied by the reference."""
    def __init__(
        self,
        context,
        oids,
        objectmap,
        reftype,
        ignore_missing,
        resolve,
        orientation,
        ):
        self.context = context
        self.oids = oids
        self.objectmap = objectmap
        self.reftype = reftype
        self.ignore_missing = ignore_missing
        self.resolve = resolve
        self.orientation = orientation

    def __nonzero__(self):
        """ Returns ``True`` if there are oids associated with this
        multireference, ``False`` if the oid list is empty. """
        return bool(self.oids)

    def __getitem__(self, i):
        """ Return the i'th element from the sequence of objects or object
        ids"""
        oid = self.oids[i]
        if self.resolve:
            return self.objectmap.object_for(oid)
        return oid

    def __contains__(self, other):
        """ Return ``True`` if ``other`` is a member of the sequence managed
        by this multireference. """
        if self.resolve:
            object_for = self.objectmap.object_for
            for oid in self.oids:
                if object_for(oid) == other:
                    return True
            return False
        return other in self.oids

    def __iter__(self):
        """ Return an iterable of object ids or objects. """
        if self.resolve:
            return iter((self.objectmap.object_for(oid) for oid in self.oids))
        return iter(self.oids)

    def __len__(self):
        """ Return the length of the sequence of objects implied by this
        multireference"""
        return len(self.oids)

    def connect(self, objects, ignore_missing=None):
        """ Connect ``objects`` to this reference's relationship. ``objects``
        should be a sequence of content objects or object identifiers."""
        if ignore_missing is None:
            ignore_missing = self.ignore_missing
        ctx_oid = oid_of(self.context)
        for obj in objects:
            try:
                if self.orientation == 'source':
                    self.objectmap.connect(ctx_oid, obj, self.reftype)
                else:
                    self.objectmap.connect(obj, ctx_oid, self.reftype)
            except ValueError:
                if not ignore_missing:
                    raise

    def disconnect(self, objects, ignore_missing=None):
        """ Disonnect ``objects`` to this reference's relationship. ``objects``
        should be a sequence of content objects or object identifiers."""
        if ignore_missing is None:
            ignore_missing = self.ignore_missing
        ctx_oid = oid_of(self.context)
        for obj in objects:
            try:
                if self.orientation == 'source':
                    self.objectmap.disconnect(ctx_oid, obj, self.reftype)
                else:
                    self.objectmap.disconnect(obj, ctx_oid, self.reftype)
            except ValueError:
                if not ignore_missing:
                    raise

    def clear(self):
        """ Clear all references in this relationship. """
        self.disconnect(self.oids)

def find_objectmap(context):
    """ Returns the object map for the root object in the lineage of the
    ``context``"""
    return acquire(context, '__objectmap__')
            
def includeme(config): # pragma: no cover
    config.scan('.')
    
