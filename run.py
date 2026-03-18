"""Entry point for development server and Gunicorn."""
import click
from app import create_app

app = create_app()


@app.cli.command("make-admin")
@click.argument("email")
def make_admin(email):
    """Grant admin rights to a user by email."""
    from app.extensions import db
    from app.models.user import User
    user = User.query.filter_by(email=email).first()
    if not user:
        click.echo(f"User '{email}' not found.")
        return
    user.is_admin = True
    db.session.commit()
    click.echo(f"✅ User '{email}' is now admin.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
