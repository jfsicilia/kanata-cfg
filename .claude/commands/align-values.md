---
name: align-values
description: Align all variable values vertically in specified kanata .kbd files
---

Align all variable values vertically in the specified kanata .kbd file(s).

Find every line that assigns a variable (pattern: `varname  value`) and align all values to a consistent column based on the longest variable name plus padding. Skip multi-line expressions (where opening parens exceed closing parens on the same line) and comment/structural lines.

The target file(s): $ARGUMENTS

If no arguments are provided, ask which file(s) to align.
