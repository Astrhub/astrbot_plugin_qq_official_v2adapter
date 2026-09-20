"""Stable in-process failure contract; no fabricated action data."""


class V2Error(RuntimeError):
    def __init__(self, code: str, message: str, *, retcode: int = 1400,
                 status: int = 400, business_code=None, trace_id=None,
                 retry_after=None, phase="not_sent", http_status=None, operation_id=None):
        super().__init__(message)
        self.code = code
        self.retcode = retcode
        self.status = status
        self.http_status = http_status
        self.business_code = business_code
        self.trace_id = trace_id
        self.retry_after = retry_after
        self.phase = phase
        self.operation_id = operation_id

    def as_dict(self):
        return {"status": "failed", "retcode": self.retcode, "data": None,
                "code": self.code, "message": str(self),
                "business_code": self.business_code, "trace_id": self.trace_id, "http_status": self.http_status,
                "retry_after": self.retry_after, "phase": self.phase, "operation_id": self.operation_id}


def unsupported(message="This capability is not implemented in the prototype."):
    return V2Error("unsupported", message, retcode=1404, status=501)


def not_ready():
    return V2Error("transport_not_ready", "This client has no active QQ transport.", status=503)
