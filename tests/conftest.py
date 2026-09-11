import sys
from pathlib import Path

# 确保项目根（含 core/、boss_state 等）在 import 路径上
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
