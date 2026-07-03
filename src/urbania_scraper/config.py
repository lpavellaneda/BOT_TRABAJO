import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass
class ScraperConfig:
    search_url: str
    pages: int = 1
    output_format: str = "csv"
    output_path: str = "output/urbania_results.csv"

    @classmethod
    def from_env(cls) -> "ScraperConfig":
        return cls(
            search_url=os.getenv("URBANIA_SEARCH_URL", "https://urbania.pe/buscar"),
            pages=int(os.getenv("PAGES", "1")),
            output_format=os.getenv("OUTPUT_FORMAT", "csv").lower(),
            output_path=os.getenv("OUTPUT_PATH", "output/urbania_results.csv"),
        )
