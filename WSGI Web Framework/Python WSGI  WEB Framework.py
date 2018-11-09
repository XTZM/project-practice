from wsgiref.simple_server import make_server
from webob import Request, Response, dec, exc
import re

'''
http://127.0.0.1:9999/
http://127.0.0.1:9999/python/12
http://127.0.0.1:9999/python/python
'''

class DictObj:
    def __init__(self, d: dict):
        if isinstance(d, (dict,)):
            self.__dict__['_dict'] = d
        else:
            self._dict = d

    def __getattr__(self, item):
        try:
            return self._dict[item]
        except KeyError:
            KeyboardInterrupt('Attribute {} Not Found'.format(item))

    def __setattr__(self, key, value):
        # 不允许设置属性
        raise NotImplementedError


class Context(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError('Attribute {} Not Found.'.format(item))

    def __setattr__(self, key, value):
        self[key] = value


class NestedContext(Context):
    def __init__(self, globalcontext:Context=None):
        super().__init__()
        self.globalcontext = globalcontext

    def relate(self, globalcontext:Context=None):
        self.globalcontext = globalcontext

    def __getattr__(self, item):
        if item in self.keys():
            return self[item]
        return self.globalcontext[item]

class Router:
    KVPATTERN = re.compile('/({[^{}:]+:?[^{}:]*})')
    TYPEPATTERNS = {
        'str': r'[^/]+',
        'word': r'\w+',
        'int': r'[+-]?\d+',
        'float': r'[+-]?\.\d+',  # 严苛的要求必须是15.6这样的形式
        'any': r'.+'
    }

    TYPECAST = {
        'str': str,
        'word': str,
        'int': int,
        'float': float,
        'any': str
    }

    def transform(self, kv: str):
        # /{id:int} => /(?P<id>[+-]?\d+)
        name, _, type = kv.strip('/{}').partition(':')
        # 返回元组，（目标正则表达式，被替换部分类型有序列表）
        return '/(?P<{}>{})'.format(name, self.TYPEPATTERNS.get(type, '\w+')), name, self.TYPECAST.get(type, str)

    def parse(self,src: str):
        start = 0  # '/({[^{}:]+:?[^{}:]*})'
        res = ''  # s = '/student/{name:str}/xxx/{id:int}' /prefix/{name}/{id}
        translator = {}  # id =>int  name =>str
        while True:
            matcher = self.KVPATTERN.search(src, start)
            if matcher:
                res += matcher.string[start: matcher.start()]
                tmp = self.transform(matcher.string[matcher.start(): matcher.end()])
                res += tmp[0]
                translator[tmp[1]] = tmp[2]
                start = matcher.end()
            else:

                break
            # 没有任何匹配应该原样返回字符串
        if res:
            return res, translator
        else:
            return src, translator

    def __init__(self, prefix: str=""):
        self.__prefix = prefix.rstrip('/\\')  # 前缀，例如/product
        # /python/python
        self.__routeable = []  # 三元组

        # 未绑定全局的上下文
        self.ctx = NestedContext()

        # 拦截器
        self.preinterceptor = []
        self.postinterceptor = []

    # 拦截器注册函数
    def reg_preinterceptor(self, fn):
        self.preinterceptor.append(fn)
        return fn

    def reg_postinterceptor(self, fn):
        self.postinterceptor.append(fn)
        return fn


    def get(self, pattern):
        return self.route(pattern, 'GET')

    def post(self, pattern):
        return self.route(pattern, 'POST')

    def head(self, pattern):
        return self.route(pattern, 'HEAD')

    @property
    def prefix(self):
        return self.__prefix

    def route(self, rule, *methods):
        def wrapper(handler):
            pattern, translator = self.parse(rule)
            self.__routeable.append((methods, re.compile(pattern), translator, handler))
            return handler
        return wrapper

    def match(self, request: Request) -> Response:
        # 前缀处理，prefix是一级的，属于你管的prefix
        if not request.path.startswith(self.prefix):
            return None

        # 依次执行拦截请求
        for fn in self.preinterceptor:
            request = fn(self.ctx, request)
        for methods, pattern, translator, handler in self.__routeable:
            # not methods表示一个方法都没有定义，就是支持全部方法
            if not methods or request.method.upper() in methods:
                # 保证是prefix开头，所以可以replace
                # 去掉prefix剩下的才是正则表达式匹配的路径
                matcher = pattern.search(request.path.replace(self.prefix, "", 1))
                if matcher:
                    # request.kwargs = DictObj(matcher.groupdict())  # 所有的命名的分组
                    newdict = {}
                    for k, v in matcher.groupdict().items():
                        newdict[k] = translator[k](v) # 将id使用int转换
                    request.vars = DictObj(newdict)  # request.vars.id  request.vars.name
                    response = handler(self.ctx)  # 增加上下文

                    # 依次执行拦截响应
                    for fn in self.postinterceptor:
                        response = fn(self.ctx, request, response)

                    return response



class Application:
    ctx = Context()  # 全局上下文对象

    def __init__(self, **kwargs):
        # 创建上下文对象，共享信息
        self.ctx.app = self
        for k, v in kwargs.items():
            self.ctx[k] = v

    ROUTERS = []  # 前缀开头的所有Router对象

    # 拦截器
    PREINTERCEPTOR = []
    POSTINTERCEPTOR = []

    # 拦截器注册函数
    @classmethod
    def reg_preinterceptor(cls, fn):
        cls.PREINTERCEPTOR.append(fn)
        return fn

    @classmethod
    def reg_postinterceptor(cls,fn):
        cls.POSTINTERCEPTOR.append(fn)
        return fn

    @classmethod
    def register(cls, router:Router):
        # 为Router实例注入全局上下文
        router.ctx.relate(cls.ctx)
        router.ctx.router = router
        cls.ROUTERS.append(router)
        return router

    @classmethod
    def extend(cls, name, ext):
        cls.ctx[name] = ext


    @dec.wsgify
    def __call__(self, request: Request) -> Response:
        # 全局拦截请求
        for fn in self.PREINTERCEPTOR:
            request = fn(self.ctx, request)

        # 遍历ROUTERS，调用Router实例的match方法，看匹配谁
        for router in self.ROUTERS:
            response = router.match(request)
            if response:  # 匹配返回非None的Router对象
                # 全局拦截响应
                for fn in self.POSTINTERCEPTOR:
                    response = fn(self.ctx, request, response)
                return response  # 匹配则立即返回
        raise exc.HTTPNotFound("你访问的页面不见了")


# 创建Router
idx = Router()
py = Router('/python')

# 一定要注册
Application.register(idx)
Application.register(py)


@idx.get('^/$')  # 只匹配根
def index(request: Request):
    res = Response()
    res.body = "<h1>测试用例1</h1>".encode()
    return res


# @py.get('/{id:int}')
@py.get('/python')
def showpython(request: Request):
    res = Response()
    res.body = "<h1>测试用例2</h1>".encode()
    return res

# 拦截器举例
@Application.reg_preinterceptor
def showheaders(ctx:Context, request:Request) -> Request:
    print(request.path)
    print(request.user_agent)
    return request

@py.reg_preinterceptor
def showprefix(ctx:Context, request:Request) -> Request:
    print('-----prefix={}'.format(ctx.router.prefix))
    return request





if __name__ == "__main__":
    ip = '127.0.0.1'
    port = 9999
    server = make_server(ip, port, Application())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        server.server_close()

