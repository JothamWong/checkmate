from abc import abstractmethod, ABC


class BaseOperatorState(ABC):

    def __init__(self, operator_names: set[str]):
        self.operator_names = operator_names

    @abstractmethod
    async def put(self, key, value, operator_name: str):
        raise NotImplementedError

    @abstractmethod
    async def get(self, key, operator_name: str):
        raise NotImplementedError

    @abstractmethod
    async def delete(self, key, operator_name: str):
        raise NotImplementedError

    @abstractmethod
    async def exists(self, key, operator_name: str):
        raise NotImplementedError
