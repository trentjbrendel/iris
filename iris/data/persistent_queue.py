"""A queue object that persists to disk as a pickle with each operation."""
import pickle
from collections import deque


class PersistentQueue(deque):
    """A disk-persistent queue.

    A deque-based object which functions like a FIFO queue and caches to
    disk on each call. This object is not thread-safe and will have unexpected
    behavior if shared between threads.

    The intended / expected behavior is as follows:
        - put() adds items to the end of the queue and persists it to disk.

        - the user will use peek() to nondestructively retrieve the top item
            on the queue.  When a task using that data is complete,
            mark_done() will be called, which will persist the queue to disk.

        - use of get() destructively returns the first item from the queue and
            persists it to disk.  Use this where the consuming code of that
            object will not fail.

    Attributes:
        path (Path): path on disk of the queue
        q (deque): double ended queue object

    """

    def __init__(self, path):
        """Create a new PersistentQueue.

        Parameters
        ----------
        path : str
            where to persist the queue too.

        """
        self.path = path
        try:
            with open(self.path, mode='rb') as file:
                self.q = pickle.load(file)
        except (FileNotFoundError, IOError):
            self.q = deque()

    def persist(self):
        """Persist the queue to disk."""
        with open(self.path, mode='wb') as file:
            pickle.dump(self.q, file, protocol=-1)

    def put(self, item):
        """Put an item on the end of the queue.

        Args:
            item (object): an item
        """
        self.q.append(item)
        self.persist()

    def get(self):
        """Return the leftmost item on the queue and remove it.

        Returns:
            object: leftmost item in queue.

        """
        return self.q.popleft()
        self.persist()

    def peek(self):
        """Return the leftmost item on the queue without removing it.

        Returns:
            object: leftmost item in queue.

        """
        item = self.q.popleft()
        self.q.appendleft(item)
        return item

    def mark_done(self):
        """Remove the leftmost item from the queue."""
        self.q.popleft()
        self.persist()