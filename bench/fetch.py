"""Clone the target library at the pinned commit: python -m bench.fetch"""
from .runner import TARGET_COMMIT, fetch_base

if __name__ == "__main__":
    print("base repo ready at", fetch_base(), "commit", TARGET_COMMIT)
