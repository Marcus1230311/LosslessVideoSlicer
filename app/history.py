class History:
    def __init__(self, max_items=100):
        self.max_items=max_items; self.undo_stack=[]; self.redo_stack=[]
    def clear(self): self.undo_stack.clear(); self.redo_stack.clear()
    def push(self, snapshot):
        self.undo_stack.append(snapshot)
        if len(self.undo_stack)>self.max_items: self.undo_stack.pop(0)
        self.redo_stack.clear()
    def undo(self, current):
        if not self.undo_stack: return None
        snap=self.undo_stack.pop(); self.redo_stack.append(current); return snap
    def redo(self, current):
        if not self.redo_stack: return None
        snap=self.redo_stack.pop(); self.undo_stack.append(current); return snap
