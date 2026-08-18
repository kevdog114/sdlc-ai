import sys
from pathlib import Path
import json
import time

# Add project root to path
BASE_DIR = Path("/Users/klschaefer/dev-projects/sdlc-ai")
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "tools"))

from tools.project_tool import _load_project, _save_project, _create_stories_and_tasks, _execute_story_tasks

def main():
    project_id = "proj-1ba32e51"
    print(f"Starting full execution loop for project: {project_id}")
    
    # Debugging: check if we can find the folder manually
    root_dir = Path("/Users/klschaefer/dev-projects/sdlc-ai/user_projects")
    print(f"DEBUG: Looking in root directory: {root_dir}")
    if not root_dir.exists():
        print(f"ERROR: Root directory {root_dir} does not exist!")
    else:
        print(f"DEBUG: Found directories: {[d.name for d in root_dir.iterdir() if d.is_dir()]}")

    project = _load_project(project_id)
    if not project:
        # Try to find it manually as a fallback/debug
        print(f"DEBUG: _load_project failed to find {project_id}. Attempting manual scan...")
        found = False
        for folder in root_dir.iterdir():
            if folder.is_dir():
                state_file = folder / "state.json"
                if state_file.exists():
                    try:
                        with open(state_file, 'r') as f:
                            data = json.load(f)
                            if data.get("id") == project_id:
                                print(f"DEBUG: FOUND IT manually in {folder}")
                                project = data
                                found = True
                                break
                    except Exception as e:
                        print(f"DEBUG: Error reading {state_file}: {e}")
        if not found:
            print(f"ERROR: Project {project_id} truly not found after manual scan.")
            return

    print(f"Successfully loaded project: {project['name']}")
    print(f"Current Phase: {project.get('phase')}")

    # 1. Regenerate the full story objects (since we only saved titles earlier)
    print("Regenerating story objects from architecture...")
    stories = _create_stories_and_tasks(
        architecture=project["architecture"],
        refined_requirements=project["refined_requirements"],
        interface_spec=project.get("interface_spec", ""),
    )

    if not stories:
        print("Error: Could not regenerate stories.")
        return

    print(f"Found {len(stories)} stories to execute.\n")

    # 2. Loop through and execute each story
    for idx, story in enumerate(stories):
        print(f"\n🚀 [Story {idx + 1}/{len(stories)}] Executing: {story['title']}")
        
        try:
            result = _execute_story_tasks(
                story=story,
                project_id=project_id,
                interface_spec=project.get("interface_spec", ""),
            )
            
            # Add to results
            if "results" not in project:
                project["results"] = []
                
            result["title"] = story["title"]
            project["results"].append(result)
            _save_project(project)
            print(f"✅ Story {idx+1} finished. Result: {json.dumps(result, indent=2, default=str)}")
        except Exception as e:
            print(f"❌ Story {idx+1} failed with error: {e}")
            project["results"].append({"title": story["title"], "success": False, "error": str(e)})
            _save_project(project)
            break 

    # Check if finished or need to update phase
    if len(project.get("results", [])) == len(stories):
        print("\n--- All stories processed! Finalizing project ---")
        project["phase"] = "complete"
        project["status"] = "completed"
        _save_project(project)
    else:
        print("\n--- Loop interrupted or some stories failed. Progress saved. ---")

    print("\nFinal Project State:")
    print(json.dumps(_load_project(project_id), indent=2, default=str))

if __name__ == "__main__":
    main()
