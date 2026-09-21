"""저장소 루트를 import 경로에 둔다.

`pytest` 를 어느 디렉터리에서 부르든 `nh_parser_fin` 을 찾게 한다. 패키지를
설치(`pip install -e .`)했다면 없어도 되지만, 받자마자 테스트부터 돌려 보는
경우가 많아 그 경로를 막지 않는다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
