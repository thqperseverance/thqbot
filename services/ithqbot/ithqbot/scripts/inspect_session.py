
import sys
import json
from pathlib import Path
from ithqbot.session.factory import create_session_store
from ithqbot.session.manager import SessionManager
from ithqbot.config.loader import load_config
from ithqbot.config.paths import get_default_config_path

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "./config.json"
    config = load_config(config_path)
    workspace = Path(config.workspace_path)
    
    store_uri = config.get_active_session_store_uri()
    print(f"Using store URI: {store_uri}")
    
    store = create_session_store(workspace, config_uri=store_uri)
    manager = SessionManager(workspace=workspace, store=store)
    
    # List sessions to find the recent one
    sessions_info = manager.list_sessions()
    if not sessions_info:
        print("No sessions found.")
        return
        
    print(f"Found {len(sessions_info)} sessions.")
    # Sort by updated_at
    sessions_info.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
    
    for s_info in sessions_info[:3]:
        key = s_info['key']
        print(f"\n--- Session: {key} (Updated: {s_info.get('updated_at')}) ---")
        session = manager.get_or_create(key)
        history = session.get_history(max_messages=10)
        for msg in history:
            role = msg.get('role')
            content = msg.get('content', '')
            metadata = msg.get('metadata')
            print(f"[{role}] {content[:100]}{'...' if len(content) > 100 else ''}")
            if metadata:
                print(f"  Metadata keys: {list(metadata.keys())}")
                if 'file_meta' in metadata:
                    print(f"  File: {metadata['file_meta'].get('name')} -> {metadata['file_meta'].get('rel_path')}")
                if 'attachments' in metadata:
                    print(f"  Attachments: {len(metadata['attachments'])}")

if __name__ == "__main__":
    main()
