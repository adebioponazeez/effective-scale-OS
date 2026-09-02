class Registry:
    def __init__(self):
        self._items = {}
    def register(self, item_id, item):
        self._items[item_id] = item
    def get(self, item_id):
        return self._items[item_id]
    def all(self):
        return list(self._items.values())
    def ids(self):
        return list(self._items)
