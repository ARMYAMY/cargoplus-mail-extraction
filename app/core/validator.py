import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import jsonschema
from app.config import settings

logger = logging.getLogger(__name__)


class CargoValidator:
    def __init__(self, schema_path: Optional[str] = None):
        if schema_path is None:
            schema_file = settings.skill_path / "schemas" / "output.schema.json"
        else:
            schema_file = Path(schema_path)
            
        if schema_file.exists():
            try:
                self.schema = json.loads(schema_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"Failed to load schema from {schema_file}: {e}")
                self.schema = {}
        else:
            logger.warning(f"Schema file not found at {schema_file}")
            self.schema = {}

    def validate(self, data: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate data against V3 JSON Schema. Returns (is_valid, error_list)."""
        if not self.schema:
            return True, []
        
        errors = []
        validator = jsonschema.Draft202012Validator(self.schema)
        for error in validator.iter_errors(data):
            errors.append(f"{error.json_path}: {error.message}")
        
        return len(errors) == 0, errors


default_validator = CargoValidator()
