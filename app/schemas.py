from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

ShortText = Annotated[str, Field(min_length=1, max_length=150)]
PositiveID = Annotated[int, Field(gt=0)]
Quantity = Annotated[int, Field(ge=1, le=6)]


class InputModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class Credentials(InputModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = value.lower()
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("Enter a valid email address.")
        return value


class Register(Credentials):
    name: str = Field(min_length=2, max_length=100)


class VenueInput(InputModel):
    id: int | None = None
    name: ShortText
    location: str = Field(min_length=1, max_length=200)
    capacity: int = Field(ge=1, le=100000)
    description: str = Field(default="", max_length=5000)
    image: str = Field(default="", max_length=2000)
    hourly_rate: int = Field(ge=0, le=100000000)
    private_hire: bool = True

    @field_validator("image")
    @classmethod
    def image_url(cls, value):
        if value and not value.startswith("https://"):
            raise ValueError("Image must be an HTTPS URL, or empty.")
        return value


class Times(InputModel):
    # UTC Unix timestamps, in seconds.
    start: float = Field(gt=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end <= self.start:
            raise ValueError("End time must be after start time.")
        if self.end - self.start > 7 * 86400:
            raise ValueError("An activity may last at most seven days.")
        return self


class TierInput(InputModel):
    name: str = Field(min_length=1, max_length=80)
    price: int = Field(ge=0, le=100000000)
    quantity: int = Field(ge=1, le=100000)


class EventInput(Times):
    title: ShortText
    category: str = Field(min_length=1, max_length=80)
    venue_id: PositiveID
    description: str = Field(default="", max_length=5000)
    status: Literal["draft", "published"] = "draft"
    tiers: list[TierInput] = Field(min_length=1, max_length=20)


class HireInput(Times):
    venue_id: PositiveID
    purpose: str = Field(min_length=3, max_length=2000)
    guests: int = Field(ge=1, le=100000)


class BookInput(InputModel):
    tier_id: PositiveID
    quantity: Quantity
    simulate_payment: Literal[True]


class JoinInput(InputModel):
    tier_id: PositiveID
    quantity: Quantity


class ClaimInput(InputModel):
    id: PositiveID
    simulate_payment: Literal[True]


class IDInput(InputModel):
    id: PositiveID


class HireDecision(InputModel):
    id: PositiveID
    approve: bool


class ScanInput(InputModel):
    event_id: PositiveID
    code: str = Field(min_length=1, max_length=100)


class Action(InputModel):
    action: Literal[
        "venue",
        "event",
        "publish",
        "cancel_event",
        "book",
        "cancel_booking",
        "join",
        "claim",
        "hire",
        "hire_decision",
        "scan",
    ]
    data: dict


class Message(InputModel):
    sender: str
    receiver: str
    performative: Literal[
        "REQUEST", "AGREE", "REFUSE",
        "PROPOSE", "INFORM", "FAILURE",
    ]
    content: dict
    conversation_id: str