# skookumnet-client

skookumnet-client is a Python application that connects to a [SkookumLogger](https://www.dogparksoftware.com/SkookumLogger.html)
contest logging session over SkookumNet (Apple MultipeerConnectivity) and reacts to QSO events in
real time.

SkookumLogger exposes a peer-to-peer network called SkookumNet that carries live contest data — QSOs
as they are logged, peer operating state, and activity updates. skookumnet-client joins that network
as a passive peer and delivers each event to a set of plugins you configure.

**Use cases:**

- Export contest QSOs to a secondary logging application (e.g. MacLoggerDX via the built-in ADIF
  importer plugin)
- Drive real-time scoreboards or rate displays
- Bridge SkookumNet events to other tools or protocols
- Log and archive contest activity for post-contest analysis

Plugins are plain Python classes; the included `ExamplePlugin` and `SkookumPacketPlugin` base class
make it straightforward to add new integrations.

# Virtual Environment

Uses [uv](https://docs.astral.sh/uv/) for dependency management. To install:

```
make install
```

# Running skookumnet-client

```
./skookumnet-client [args]
```

# Configuration

You can customize `skookumnet.ini` to enable plugins and specify a custom SkookumNet name.

If the `--config` option is specified on the command line, its argument will be used as the config
file. Otherwise if the `SKOOKUMNET_CLIENT_INI` environment variable is set, its value will be used
as the config file. As a backup, `skookumnet.ini` from the installation directory is used.

# MacLoggerDX ADIF Importer

Enable the `MacLoggerDX_ADIF_Importer` plugin in `skookumnet.ini` and install the extra
dependency:

```
uv add hamutils
```
