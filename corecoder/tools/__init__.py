"""Tool registry."""

from .agent import AgentTool
from .bash import BashTool
from .edit import EditFileTool
from .glob_tool import GlobTool
from .grep import GrepTool
from .read import ReadFileTool
from .todo import TodoWriteTool
from .write import WriteFileTool
from .now import NowTool

ALL_TOOLS = [
    BashTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    TodoWriteTool(),
    AgentTool(),
    # ...原有的工具...
    NowTool(),
]

