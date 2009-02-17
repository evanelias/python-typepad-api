import httplib2
import httplib
# TODO: require 2.0+ version of simplejson that doesn't provide unicode keys
import simplejson
import logging
from urlparse import urljoin
import types
from datetime import datetime
import time
import re

from typepad.dataobject import DataObject, DataObjectMetaclass

# TODO configurable?
BASE_URL = 'http://127.0.0.1:8000/'
EMAIL    = 'mjmalone@gmail.com'
PASSWORD = 'password'

userAgent = httplib2.Http()

class NotFound(httplib.HTTPException):
    pass

class Unauthorized(httplib.HTTPException):
    pass

class BadResponse(httplib.HTTPException):
    pass

def omit_nulls(data):
    if not isinstance(data, dict):
        if not hasattr(data, '__dict__'):
            return str(data)
        data = dict(data.__dict__)
    for key in data.keys():
        # TODO: don't have etag in obj data in the first place?
        if data[key] is None or key == 'etag':
            del data[key]
    return data

class Link(object):

    """A RemoteObject attribute that links from a source object to a related
    target object.

    For example, for an asset with comments, the comments list resource can be
    a `Link` from the asset with a type of `List(Comment)`. The asset is the
    source object and the comments list is the target object.

    Links are declared on the source class as attributes, but become callable
    methods. For example, if the Link is installed on `Asset` as `comments`,
    then `Asset.comments` will be the callable method that fetches that
    asset's comments list.

    """

    def __init__(self, url, expect):
        """Sets the Link's url and the type of resource at that URL.

        Parameter `url` is the URL from which the related resource can be
        fetched. If `url` is relative, it is interpreted as relative to the
        source object's URL.

        `url` can also be a callable function that returns the URL of the
        target resource, given either the source object or, if the function
        accepts variable keyword argument sets (`**kwargs`), the source object
        and the extra keyword arguments passed to the link method.

        """
        self.url    = url
        self.expect = expect

    def __call__(self, obj, **kwargs):
        """Fetches the remote resource the Link links to.

        Parameter `obj` is the existing related object (the asset object in
        the asset-comments example). Any other keyword arguments are passed to
        `RemoteObject.get()`, or if the Link's `url` is a callable function
        that accepts variable keyword argument sets, to the Link's `url`
        function too.

        """
        if callable(self.url):
            # Only give the url function the arguments it expects.
            import inspect
            if inspect.getargspec(self.url)[2] is not None:
                url = self.url(obj, **kwargs)
            else:
                url = self.url(obj)
        else:
            if getattr(obj, '_id') is None:
                raise ValueError, "The object must have an identity URL before you can follow its link"
            url = urljoin(obj._id, self.url)
        return self.expect.get(url, **kwargs)

class RemoteObjectMetaclass(DataObjectMetaclass):
    def __new__(cls, name, bases, attrs):
        # TODO: refactor with DataObjectMetaclass? urgh
        links = {}

        for base in bases:
            if isinstance(base, RemoteObjectMetaclass):
                links.update(base.links)

        for attrname, link in attrs.items():
            if isinstance(link, Link):
                links[attrname] = link
                # Replace the Link with a new method instead of deleting it.
                def make_method(linkobj):
                    def method(self, **kwargs):
                        return linkobj(self, **kwargs)
                    return method
                attrs[attrname] = make_method(link)

        attrs['links'] = links
        return super(RemoteObjectMetaclass, cls).__new__(cls, name, bases, attrs)

class RemoteObject(DataObject):

    """A DataObject that can be fetched and put over HTTP through a REST
    API."""

    __metaclass__ = RemoteObjectMetaclass

    @staticmethod
    def _raise_response(response, classname, url):
        # Turn exceptional httplib2 responses into exceptions.
        if response.status == httplib.NOT_FOUND: 
            raise NotFound('No such %s %s' % (classname, url))
        if response.status == httplib.UNAUTHORIZED:
            raise Unauthorized('Not authorized to fetch %s %s' % (classname, url))
        # catch other unhandled
        if response.status != httplib.OK:
            raise BadResponse('Bad response fetching %s %s: %d %s' % (classname, url, response.status, response.reason))
        if response.get('content-type') != 'application/json':
            raise BadResponse('Bad response fetching %s %s: content-type is %s, not JSON' % (classname, url, response.get('content-type')))

    @classmethod
    def get(cls, url, http=None, **kwargs):
        """Fetches a RemoteObject from a URL.

        Parameter `url` is the URL from which the object should be gotten.
        Optional parameter `http` is the user agent object to use for
        fetching. `http` should be compatible with `httplib2.Http` objects.

        """
        logging.debug('Fetching %s' % (url,))

        if http is None:
            http = userAgent
        (response, content) = http.request(url)
        cls._raise_response(response, classname=cls.__name__, url=url)
        logging.debug('Got content %s' % (content,))

        # TODO make sure astropad is returning the proper content type
        #if data and resp.get('content-type') == 'application/json':
        data = simplejson.loads(content)
        x = cls.from_dict(data)
        x._id = response['content-location']  # follow redirects
        if 'etag' in response:
            x._etag = response['etag']
        return x

    def save(self, http=None):
        """Save a RemoteObject to a remote resource.

        If the RemoteObject was fetched with a `get()` call, it is saved by
        HTTP `PUT` to the resource's URL. If the RemoteObject is new, it is
        saved through a `POST` to its parent collection.

        Optional `http` parameter is the user agent object to use. `http`
        objects should be compatible with `httplib2.Http` objects.

        """
        if http is None:
            http = userAgent
        http.add_credentials(EMAIL, PASSWORD)

        body = simplejson.dumps(self.to_dict(), default=omit_nulls)

        httpextra = {}
        if self._id is not None:
            url = self._id
            method = 'PUT'
            if hasattr(self, _etag) and self._etag is not None:
                httpextra['headers'] = {'if-match': self._etag}
        elif self.parent is not None and self.parent._id is not None:
            url = self.parent._id
            method = 'POST'
        else:
            # FIXME: !
            url = urljoin(BASE_URL, '/blogs/1/posts.json')
            method = 'POST'
            # raise ValueError('nowhere to save this object to?')

        (response, content) = http.request(url, method=method, body=body, **httpextra)

        # TBD: check for errors
        # self._raise_response(response, classname=type(self).__name__, url=url)

        # TODO: follow redirects first?
        logging.debug('Yay saved my obj, now turning %s into new content' % (content,))
        new_body = simplejson.loads(content)
        new_inst = type(self).from_dict(new_body)
        self.__dict__.update(new_inst.__dict__)
        self._id = response['content-location']
        if 'etag' in response:
            self._etag = response['etag']
