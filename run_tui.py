#!/usr/bin/env python3
"""Entry point script for the Projects TUI."""

from projection.app import ProjectsApp


def main():
    app = ProjectsApp()
    app.run()


if __name__ == "__main__":
    main()
