# Local Config

`router.example.yaml` is the public, non-secret router configuration template.

`router.prod.local.yaml` is the default local ignored router config used by scripts when `ROUTER_CONFIG` is not set.

`router.sit.local.yaml` is an optional local ignored test/SIT router config. Use it by setting `ROUTER_CONFIG=config/router.sit.local.yaml`.

`chfs.example.yaml` is the public, non-secret CHFS configuration template.

`chfs.sit.local.yaml` is the local ignored CHFS config for downloading strategy backtest trade logs.

Do not commit local config files because they contain credentials.

Connectivity may require the proper VPN/network environment.
