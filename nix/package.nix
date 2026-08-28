{
  lib,
  python3Packages,
}:
python3Packages.buildPythonApplication {
  pname = "chat-cards";
  version = "0.1.0";
  pyproject = true;

  # Only the sources the build actually needs. Without this filter the whole
  # working tree — .direnv included — lands in the store on every rebuild.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../src
    ];
  };

  build-system = [python3Packages.setuptools];
  dependencies = [python3Packages.aiohttp];

  pythonImportsCheck = ["chat_cards"];
  doCheck = false;

  meta = with lib; {
    description = "Trading-card pack-opening scanner and Twitch overlay, backed by CardSight AI";
    mainProgram = "chat-cards";
    platforms = platforms.linux;
    license = licenses.mit;
  };
}
