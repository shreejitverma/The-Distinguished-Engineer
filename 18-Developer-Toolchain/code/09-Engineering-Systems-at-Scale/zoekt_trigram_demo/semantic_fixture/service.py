from models import Config, Template


def parse(blob):
    # parse is also a free function here
    return blob.upper()


def load(path):
    cfg = Config()
    tpl = Template()
    first = cfg.parse(path)
    second = tpl.parse(path)
    third = parse(path)
    return first, second, third


def reload(cfg: Config, text):
    return cfg.parse(text)
