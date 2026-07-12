def fix(f):
    lines = open(f, "rb").read().decode("utf-8").split("\n")
    lines = [line.rstrip(" \t\r") + ("\r" if line.endswith("\r") else "") for line in lines]
    open(f, "wb").write("\n".join(lines).encode("utf-8"))
fix("src/scorerecord/cli.py")
fix("src/scorerecord/evaluate/market_residual.py")
