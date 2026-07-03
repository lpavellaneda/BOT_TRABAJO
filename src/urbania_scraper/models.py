from dataclasses import dataclass, asdict
from typing import Optional

@dataclass
class Listing:
    property_type: Optional[str] = None
    price: Optional[str] = None
    location: Optional[str] = None
    total_area: Optional[float] = None
    covered_area: Optional[float] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    parking: Optional[int] = None
    advertiser: Optional[str] = None
    description: Optional[str] = None
    detail_url: Optional[str] = None
    amenities: Optional[str] = None
    delivery_status: Optional[str] = None
    delivery_date: Optional[str] = None
    age: Optional[str] = None
    condition: Optional[str] = None
    latitude: Optional[str] = None
    longitude: Optional[str] = None

    def to_dict(self):
        return asdict(self)
