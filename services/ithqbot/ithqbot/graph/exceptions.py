class GraphError(Exception):
    pass


class GraphValidationError(GraphError):
    pass


class GraphExecutionError(GraphError):
    pass


class InteractionRequired(GraphError):
    def __init__(self, payload: dict):
        super().__init__("Interaction required")
        self.payload = payload
