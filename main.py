import click
import typer
from typer.core import TyperGroup

from models import completion_default


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
    typer.echo(completion_default(text))


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
