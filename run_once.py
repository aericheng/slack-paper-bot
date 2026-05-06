"""一次性執行論文總結後 exit。給 GitHub Actions cron 用，不需要 Socket Mode。"""
from main import run_summary


if __name__ == "__main__":
    run_summary()
