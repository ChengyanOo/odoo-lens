def depends(*args):
    return lambda f: f

def depends_context(*args):
    return lambda f: f

def constrains(*args):
    return lambda f: f

def onchange(*args):
    return lambda f: f

def ondelete(*, at_uninstall):
    return lambda f: f

def model(method):
    return method

def model_create_multi(method):
    return method


class Environment:
    def __getitem__(self, model_name):
        return self.registry[model_name]

    def ref(self, xml_id, raise_if_not_found=True):
        pass

    @property
    def user(self):
        return self(su=True)['res.users'].browse(self.uid)

    @property
    def company(self):
        pass
