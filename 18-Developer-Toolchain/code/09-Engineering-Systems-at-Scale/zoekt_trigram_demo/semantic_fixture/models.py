class Config:
    """Settings loaded from text."""

    def parse(self, text):
        return text.split("=")

    def render(self):
        return "Config.parse is the inverse of render"


class Template:
    def parse(self, text):
        return text.strip()
