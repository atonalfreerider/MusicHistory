MusicHistory.Layout is a new version of **GPU-FDG**
(<https://github.com/atonalfreerider/GPU-FDG>, forked at commit
`00b4448d3a99a4677fa8bfc4725d268463cca441`, "gravity and one way edges"). The force kernel,
its CSR adjacency layout, the node-edge clearance term and the overall program structure derive
from GPU-FDG, which is distributed under the MIT License reproduced below. The copyright and
permission notice must be kept with every copy or substantial portion of this code.

---

The MIT License (MIT)

Copyright (c) 2021 john

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

Third-party components used by MusicHistory.Layout (restored from NuGet, not vendored):

* [ComputeSharp](https://github.com/Sergio0694/ComputeSharp) 3.2.0 (MIT, Sergio Pedri)
* [Microsoft.Data.Sqlite](https://github.com/dotnet/efcore) 10.0.12 (MIT, .NET Foundation)
  with SQLitePCLRaw (Apache-2.0) and SQLite (public domain)
