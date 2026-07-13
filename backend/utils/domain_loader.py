from pathlib import Path
import json

BASE_DIR = Path(__file__).resolve().parent.parent

def load_text(path):
    return (BASE_DIR / path).read_text(
        encoding="utf-8"
    )

def load_json(path):
    with open(
        BASE_DIR / path,
        encoding="utf-8"
    ) as f:
        return json.load(f)

def load_domain(domain_name):

    domain_path = BASE_DIR / "temas" / domain_name

    return {
        "config": load_json(
            f"temas/{domain_name}/config.json"
        ),
        "business_context": load_text(
            f"temas/{domain_name}/business_context.txt"
        ),
        "dataset_context": load_text(
            f"temas/{domain_name}/dataset_context.txt"
        ),
        "validation_prompt": load_text(
            f"temas/{domain_name}/validation_prompt.txt"
        ),
        "sql_base_prompt": load_text(
            f"temas/{domain_name}/sql_base_prompt.txt"
        ),
        "analysis_prompt": load_text(
            f"temas/{domain_name}/analysis_prompt.txt"
        )
    }