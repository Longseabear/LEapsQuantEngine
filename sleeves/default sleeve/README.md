# default sleeve

`default sleeve` is the operational catch-all sleeve for imported broker fills that are not yet assigned to a strategy sleeve.

It should stay light:

- no alpha models by default
- no active universe by default
- same virtual account store as strategy sleeves
- used for initial migration, unassigned residual quantities, and reconciliation work

The engine should treat it like any other sleeve portfolio projection, but strategy code should not rely on it for signal generation.
