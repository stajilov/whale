import click
import typer
from typer.core import TyperGroup

from models import completion_default
from session import appendMessage, initSession
from memory import _hook_record


class DefaultCommandGroup(TyperGroup):
    """Route an unmatched positional argument to the default command."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["prompt", *args]
        return super().parse_args(ctx, args)


app = typer.Typer(cls=DefaultCommandGroup)


@app.command(hidden=True)
def prompt(text: str = typer.Argument(..., help="Prompt to execute.")) -> None:
    """Execute a prompt using the default model."""
    session = initSession()
    appendMessage(session.id, {"role": "user", "content": text})
    response = completion_default(text)
    if response is not None:
        appendMessage(session.id, {"role": "assistant", "content": response})
        _hook_record(
            {
                "kind": "turn",
                "session_id": session.id,
                "prompt": text,
                "response": response,
            }
        )
    typer.echo(response)


@app.command()
def hello(name: str) -> None:
    typer.echo(f"Hello {name}")


@app.command()
def goodbye(name: str, formal: bool = False) -> None:
    if formal:
        typer.echo(f"Goodbye Ms. {name}. Have a good day.")
    else:
        typer.echo(f"Bye {name}!")


if __name__ == "__main__":
    app()
