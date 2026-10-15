from structures import Graph, LinkedList, Queue, Stack


def test_stack():
    stack = Stack()
    stack.push("TP53")
    stack.push("BRCA1")
    assert stack.peek() == "BRCA1"
    assert stack.pop() == "BRCA1"
    assert stack.pop() == "TP53"
    assert stack.is_empty()


def test_queue():
    queue = Queue()
    queue.enqueue("Cell1")
    queue.enqueue("Cell2")
    assert queue.peek() == "Cell1"
    assert queue.dequeue() == "Cell1"
    assert queue.dequeue() == "Cell2"
    assert queue.is_empty()


def test_linked_list():
    linked = LinkedList()
    linked.append("TP53")
    linked.prepend("BRCA1")
    assert linked.search("TP53").value == "TP53"
    assert linked.delete("BRCA1")
    assert linked.search("BRCA1") is None
    assert len(linked) == 1


def test_graph_bfs():
    graph = Graph()
    graph.add_edge("TP53", "BRCA1")
    graph.add_edge("TP53", "EGFR")
    graph.add_edge("BRCA1", "KRAS")
    assert graph.bfs("TP53") == ["TP53", "BRCA1", "EGFR", "KRAS"]


def test_graph_dfs():
    graph = Graph()
    graph.add_edge("TP53", "BRCA1")
    graph.add_edge("TP53", "EGFR")
    graph.add_edge("BRCA1", "KRAS")
    assert graph.dfs("TP53") == ["TP53", "BRCA1", "KRAS", "EGFR"]
