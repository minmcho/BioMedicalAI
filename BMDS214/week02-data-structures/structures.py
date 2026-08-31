"""Core data structures for BMDS 214 Week 2."""


class Stack:
    """LIFO stack implemented with a Python list."""

    def __init__(self):
        self._items = []

    def push(self, item):
        self._items.append(item)

    def pop(self):
        if self.is_empty():
            raise IndexError("pop from empty stack")
        return self._items.pop()

    def peek(self):
        if self.is_empty():
            raise IndexError("peek from empty stack")
        return self._items[-1]

    def is_empty(self):
        return len(self._items) == 0

    def __len__(self):
        return len(self._items)


class Queue:
    """FIFO queue with a head index to avoid repeated front deletion."""

    def __init__(self):
        self._items = []
        self._head = 0

    def enqueue(self, item):
        self._items.append(item)

    def dequeue(self):
        if self.is_empty():
            raise IndexError("dequeue from empty queue")
        item = self._items[self._head]
        self._head += 1
        return item

    def peek(self):
        if self.is_empty():
            raise IndexError("peek from empty queue")
        return self._items[self._head]

    def is_empty(self):
        return self._head == len(self._items)

    def __len__(self):
        return len(self._items) - self._head


class Node:
    def __init__(self, value):
        self.value = value
        self.next = None


class LinkedList:
    """Singly linked list."""

    def __init__(self):
        self.head = None
        self._size = 0

    def prepend(self, value):
        node = Node(value)
        node.next = self.head
        self.head = node
        self._size += 1

    def append(self, value):
        node = Node(value)
        if self.head is None:
            self.head = node
        else:
            current = self.head
            while current.next is not None:
                current = current.next
            current.next = node
        self._size += 1

    def search(self, value):
        current = self.head
        while current is not None:
            if current.value == value:
                return current
            current = current.next
        return None

    def delete(self, value):
        previous = None
        current = self.head
        while current is not None:
            if current.value == value:
                if previous is None:
                    self.head = current.next
                else:
                    previous.next = current.next
                self._size -= 1
                return True
            previous, current = current, current.next
        return False

    def __len__(self):
        return self._size


class Graph:
    """Undirected graph represented by an adjacency list."""

    def __init__(self):
        self.adjacency = {}

    def add_vertex(self, vertex):
        self.adjacency.setdefault(vertex, set())

    def add_edge(self, source, target):
        self.add_vertex(source)
        self.add_vertex(target)
        self.adjacency[source].add(target)
        self.adjacency[target].add(source)

    def neighbors(self, vertex):
        return self.adjacency.get(vertex, set())

    def bfs(self, start):
        if start not in self.adjacency:
            return []
        visited = {start}
        queue = Queue()
        queue.enqueue(start)
        order = []
        while not queue.is_empty():
            vertex = queue.dequeue()
            order.append(vertex)
            for neighbor in sorted(self.adjacency[vertex]):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.enqueue(neighbor)
        return order

    def dfs(self, start):
        if start not in self.adjacency:
            return []
        visited = set()
        order = []
        stack = Stack()
        stack.push(start)
        while not stack.is_empty():
            vertex = stack.pop()
            if vertex in visited:
                continue
            visited.add(vertex)
            order.append(vertex)
            for neighbor in sorted(self.adjacency[vertex], reverse=True):
                if neighbor not in visited:
                    stack.push(neighbor)
        return order
