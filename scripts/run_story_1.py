import sys
from pathlib import Path
import json

# Add project root to path
BASE_DIR = Path("/Users/klschaefer/dev-projects/sdlc-ai")
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "tools"))

from tools.project_tool import _load_project, _save_project, _execute_story_tasks, _create_stories_and_tasks

def main():
    project_id = "proj-1ba32e51"
    project = _load_project(project_id)

    if not project:
        print(f"Error: Project {project_id} not found.")
        return

    # Regenerate stories to get full objects
    stories = _create_stories_and_tasks(
        architecture=project["architecture"],
        refined_requirements=project["refined_requirements"],
        interface_spec=project.get("interface_spec", ""),
    )

    if not stories:
        print("Error: No stories found.")
        return

    # We'll just run the first story for now to avoid massive timeouts
    story = stories[0]
    print(f"Starting execution of story 1/4: {story['title']}")
    
    result = _execute_story_tasks(
        story=story,
        project_id=project_id,
        interface_spec=project.get("interface_spec", ""),
    )

    if "results" not in project:
        project["results"] = []
    project["results"].append(result)
    _save_project(project)
    
    print(f"\nStory 1 Finished. Result Summary: {json.dumps(result, indent=2, default=str)}")

if __name__ == "__main__":
    main()
