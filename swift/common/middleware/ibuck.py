# TODO(jnoor): Limitations: 1. we are using PIL's BICUBIC algorithm for resizing into four different
# TODO(jnoor): size from the original image. Best algorithm for resizing is ANTIALIAS, but slower than BICUBIC


# The default resizing method used by thumbnail is NEAREST, which is a really bad choice.
# If you're resizing to 1/5 of the original size for example, it will output one pixel
# and throw out the next 4 - a one-pixel wide line has only a 1 out of 5 chance of
# showing up at all in the result!
#
# The surprising thing is that BILINEAR and BICUBIC aren't much better.
# They take a formula and apply it to the 2 or 3 closest pixels to
# the source point, but there's still lots of pixels they don't look at, and the formula
# will deemphasize the line anyway.
#
# The best choice is ANTIALIAS, which appears to take all of the original
# image into consideration without throwing away any pixels. The lines
# will become dimmer but they won't disappear entirely; you can do an extra step to
# improve the contrast if necessary.


import cStringIO
import PIL
from PIL import Image

from swift.common.swob import Request, \
    HTTPCreated, HTTPBadRequest, HTTPNotFound, wsgify
from swift.common.utils import split_path
from swift.common.utils import get_logger

TYPE_IMAGE_LARGE = 600
TYPE_IMAGE_MEDIUM = 300
TYPE_IMAGE_THUMB = 140
IMAGE_TYPE = ['jpeg', 'jpg', 'png', 'rif']
SAVE_IMAGE_FORMAT = 'JPEG'

MAX_FILE_SIZE = 5368709122


class ImageInvalid(Exception):
    pass


class UploadFailed(Exception):
    pass


def resize_image(dimension, im, logger):
    """
    :param dimension:  (100 / 300 /600)
    :param im:  PIL Image object
    :return: out[PIL Image object], (new_width, new_height)
    """
    new_width = new_height = dimension

    if im.size[0] <= new_width and im.size[1] <= new_height:
        new_width = im.size[0]
        new_height = im.size[1]
        out = im
    else:
        if im.size[0] <= new_width:
            new_width = im.size[0]

        w_percent = (new_width / float(im.size[0]))
        new_height = int((float(im.size[1]) * float(w_percent)))

        try:
            im.load()
        except IOError as e:
            logger.error("[IBuckMiddleware : resize_image] %s" % e)

        out = im.resize((new_width, new_height), PIL.Image.BICUBIC)

    return out, (new_width, new_height)


def crop_image(im, cimx, cimy, iw, ih):
    """
    :param im: PIL Image object
    :param cimx: X position
    :param cimy: Y position
    :param iw: width
    :param ih: height
    :return: PIL Image object
    """
    if im.size[0] <= iw + cimx and im.size[1] <= ih + cimy:
        return im

    return im.crop((cimx, cimy, cimx + iw, cimy + ih))


def upload_image(application, request_body, destination, env):
    """
    :param application:
    :param request_body: Image data
    :param destination: Upload path
    :param env: environment
    :raise: UploadFailed Exception
    """
    new_request_body_size = len(request_body.getvalue())
    if new_request_body_size > MAX_FILE_SIZE:
        raise UploadFailed("Upload Failed")

    new_env = env.copy()
    new_env['REQUEST_METHOD'] = 'PUT'
    new_env['wsgi.input'] = cStringIO.StringIO(request_body.getvalue())  # cStringIO.StringIO(''.join(request_body))
    new_env['PATH_INFO'] = destination
    new_env['CONTENT_LENGTH'] = new_request_body_size
    new_env['swift.source'] = 'PS'
    new_env['HTTP_USER_AGENT'] = \
        '%s IBuckExpand' % env.get('HTTP_USER_AGENT')

    create_obj_req = Request.blank(destination, new_env)
    resp = create_obj_req.get_response(application)

    if not resp.is_success:
        raise UploadFailed("Upload Failed")


class ImageHandler(object):
    def __init__(self, app, req, logger):
        """
        Constructor
        :param app:
        :param req:
        :raise: ValueError, HTTPNotFound, KeyAttribute, ImageInvalid
        """
        self.app = app
        self.req = req
        self.logger = logger
        self.resp_dict = {'Response Status': HTTPCreated().status,
                          'Response Body': '',
                          'Number Files Created': 0}
        self.env = req.environ
        self.resize_dimensions = [TYPE_IMAGE_LARGE, TYPE_IMAGE_MEDIUM]

        try:
            self.version, self.account, self.container, self.obj = split_path(self.req.path, 1, 4, True)
        except ValueError:
            raise HTTPNotFound(request=self.req)

        if not self.obj:
            raise ImageInvalid("Not an Image")

        if not str.lower(self.obj.split(".")[-1]) in IMAGE_TYPE:
            raise ImageInvalid("Not an Image")

        self.request_body = self.env['wsgi.input'].read(int(self.env['CONTENT_LENGTH']))
        flo = cStringIO.StringIO(self.request_body)
        try:
            self.orig_image = Image.open(flo)
        except IOError:
            raise ImageInvalid("Not an Image")

    def handle(self):
        im = self.orig_image
        # resize_data = cStringIO.StringIO(self.request_body)
        if str.lower(im.format) in ['jpeg', 'jpg']:
            resize_data = cStringIO.StringIO(self.request_body)
        else:
            resize_data = cStringIO.StringIO()
            im.save(resize_data, SAVE_IMAGE_FORMAT)

        self.obj = self.obj.replace(self.obj.split('.')[-1], 'jpg')
        # self.req.path = '/'.join(['', self.version, self.account, self.container, self.obj])
        # print(self.obj, self.req.path)
        self.env['PATH_INFO'] = '/'.join(['', self.version, self.account, self.container, self.obj])
        self.env['CONTENT_TYPE'] = 'image/jpeg'
        upload_image(self.app, resize_data, self.env['PATH_INFO'], self.env)
        resize_data.close()

        # **************** for progressive jpeg of original image
        resize_data = cStringIO.StringIO()
        try:
            im.load()
        except IOError as e:
            self.logger.error("[IBuckMiddleware : handle] %s" % e)
        im.save(resize_data, SAVE_IMAGE_FORMAT, progressive=True)
        upload_image(self.app, resize_data, '/'.join(['', self.version, self.account, self.container, 'p' + self.obj]),
                     self.env)
        resize_data.close()

        # ***************************

        for dimension in self.resize_dimensions:
            im, new_size = resize_image(dimension, im, self.logger)
            # saving new_width new_height for response
            if dimension == TYPE_IMAGE_MEDIUM:
                self.env['HTTP_X_IMW'] = str(new_size[0])
                self.env['HTTP_X_IMH'] = str(new_size[1])

            resize_data = cStringIO.StringIO()
            im.save(resize_data, SAVE_IMAGE_FORMAT)
            upload_image(self.app, resize_data,
                         '/'.join(['', self.version, self.account, self.container,
                                   ('thumb' if dimension == TYPE_IMAGE_THUMB else str(dimension)) + self.obj]),
                         self.env)
            resize_data.close()

            # **************** for progressive image of different dimensions
            resize_data = cStringIO.StringIO()
            im.save(resize_data, SAVE_IMAGE_FORMAT, progressive=True)
            upload_image(self.app, resize_data,
                         '/'.join(['', self.version, self.account, self.container,
                                   ('pthumb' if dimension == TYPE_IMAGE_THUMB else 'p' + str(dimension)) + self.obj]),
                         self.env)
            resize_data.close()


class ImageUploadHandler(ImageHandler):
    def __init__(self, app, req, logger):
        super(ImageUploadHandler, self).__init__(app, req, logger)

    def handle(self):
        self.resize_dimensions = [TYPE_IMAGE_LARGE, TYPE_IMAGE_MEDIUM, TYPE_IMAGE_THUMB]
        super(ImageUploadHandler, self).handle()


class IBuckMiddleware(object):
    """
    Middleware that makes four sub request of an uploading object to four different sizes
    """

    def __init__(self, app, conf):
        self.app = app
        self.conf = conf
        self.logger = get_logger(conf, log_route='catch-errors')
        self.image_handlers = {
            'ImageUploadHandler': ImageUploadHandler,
        }

    @wsgify
    def __call__(self, req):
        """
        this will be placed before bulk middleware
        """

        # resp = req.environ['swift.authorize'](req)
        # if not resp:
        try:

            if req.environ.get('HTTP_X_IBUCK_ENABLE', None):
                self.image_handlers['ImageUploadHandler'](self.app, req, self.logger).handle()
                resp = HTTPCreated(request=req)
            else:
                resp = self.app

        except ImageInvalid as e:
            resp = self.app
        except HTTPNotFound as e:
            resp = e
        except Exception as e:
            self.logger.error("[IBuckMiddleware] %s, req = %s " % (e, req))
            resp = HTTPBadRequest(request=req)

        return resp


def filter_factory(global_conf, **local_conf):
    conf = global_conf.copy()
    conf.update(local_conf)

    def ibuck_filter(app):
        return IBuckMiddleware(app, conf)

    return ibuck_filter
