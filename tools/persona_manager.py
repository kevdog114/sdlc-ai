import yaml
import os
from pathlib import Path
from typing import Dict, Any, List, Optional

class PersonaManager:
    def __init__(self, persona_dir: str = "personas"):
        self.persona_dir = Path(persona_dir)
        if not self.persona_dir.exists():
            raise FileNotFoundError(f"Persona directory {persona_dir} does not exist.")

    def get_persona(self, name: str) -> Dict[str, Any]:
        """Loads a persona by name."""
        file_path = self.persona_dir / f"{name}.yaml"
        if not file_path.exists():
            raise FileNotFoundError(f"Persona '{name}' not found at {file_path}")

        with open(file_path, 'r') as f:
            persona = yaml.safe_load(f)
        
        self._validate(persona)
        return persona

    def list_personas(self) -> List[str]:
        """Lists all available personas."""
        return [p.stem for p in self.persona_dir.glob("*.yaml")]

    def _validate(self, persona: Dict[str, Any]):
        """Basic structural validation of the persona schema."""
        required_fields = ['name', 'cognitive_profile', 'operational_constraints', 'behavioral_instructions']
        for field in required_fields:
            if field not in persona:
                raise ValueError(f"Missing required persona field: {field}")

# Tool wrapper for AgentRuntime
def load_persona(name: str) -> dict:
    """
    Loads a high-fidelity persona. 
    Used by agents to understand their own boundaries and expertise.
    """
    try:
        manager = PersonaManager()
        return {"success": True, "persona": manager.get_persona(name)}
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    # Test logic
    pm = PersonaManager()
    print(f"Available personas: {pm.list_personas()}")
