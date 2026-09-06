from pydantic import BaseModel, EmailStr, Field


class LeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=40)
    source: str = Field(default="website", max_length=100)
    message: str | None = Field(default=None, max_length=5000)
    # honeypot: real users never see or fill this field; bots that
    # autofill every input will trip it.
    website: str = Field(default="", max_length=200)

    @property
    def is_spam(self) -> bool:
        return bool(self.website)

    def to_payload(self) -> dict:
        return {"message": self.message} if self.message else {}
