// Syntax highlighting for the guest phone.
//
// Deliberately hand-rolled and tiny rather than a library off a CDN: the guest
// PWA is precached by the service worker and a phone on the seat PC's Wi-Fi may
// have no route to the internet at all, so anything not served by the seat is
// not there when the guest opens a diff. Everything here is one file, no deps.
//
// It is a lexer, not a parser. Each language is an ordered list of rules tried
// at the current offset; the first that matches wins and the scanner advances.
// That is enough to colour a diff hunk or a fenced block, and it degrades to
// plain escaped text for anything it does not know.

(function (global) {
  "use strict";

  // Above this a phone spends longer colouring than the guest spends reading.
  const MAX_CHARS = 40000;
  const MEMO_CHARS = 4000;
  const memo = new Map();

  function esc(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function words(list) {
    return new Set(list.trim().split(/\s+/));
  }

  const LITERALS = words(`
    true false null nil none undefined NaN Infinity True False None
  `);

  const rule = (re, cls) => ({ re, cls });

  // Identifiers carry most of the meaning, so one rule decides between keyword,
  // constant, call and type by looking at the word and the character after it.
  function ident(keywords, fold) {
    return (text, code, end) => {
      const word = fold ? text.toLowerCase() : text;
      if (keywords.has(word)) return "k";
      if (LITERALS.has(text)) return "l";
      let i = end;
      while (i < code.length && (code[i] === " " || code[i] === "\t")) i++;
      if (code[i] === "(") return "f";
      if (/^[A-Z]/.test(text) && /[a-z]/.test(text)) return "t";
      return null;
    };
  }

  const WS = rule(/[ \t\r\n]+/y, null);
  const PUNCT = rule(/[^\w\s]/y, "p");

  const KEYWORDS = {
    js: words(`
      const let var function return if else for while do break continue new class
      extends super this typeof instanceof in of async await yield import export
      from default try catch finally throw switch case delete void static get set
      interface type enum implements public private protected readonly namespace
      declare abstract as satisfies keyof infer never unknown any string number
      boolean object symbol bigint
    `),
    py: words(`
      def class return if elif else for while break continue pass import from as
      try except finally raise with lambda global nonlocal assert del yield await
      async in is not and or match case self cls print len range str int float
      bool list dict set tuple
    `),
    go: words(`
      func package import var const type struct interface map chan go defer select
      return if else for range switch case default break continue fallthrough
      make new len cap append copy delete panic recover string int int8 int16
      int32 int64 uint uint8 uint32 uint64 float32 float64 byte rune bool error
    `),
    rust: words(`
      fn let mut const static struct enum impl trait pub use mod match if else for
      while loop return break continue self Self crate super as ref move dyn where
      unsafe async await type box Some None Ok Err String Vec Option Result i8 i16
      i32 i64 u8 u16 u32 u64 usize isize f32 f64 bool str char
    `),
    java: words(`
      public private protected class interface enum extends implements static final
      abstract synchronized volatile transient native strictfp void int long short
      byte char float double boolean return if else for while do break continue new
      this super try catch finally throw throws switch case default import package
      instanceof var record sealed permits
    `),
    c: words(`
      auto break case char const continue default do double else enum extern float
      for goto if inline int long register restrict return short signed sizeof
      static struct switch typedef union unsigned void volatile while class public
      private protected virtual override final template typename namespace using
      new delete this try catch throw bool nullptr constexpr explicit friend
      operator include define ifndef endif pragma
    `),
    rb: words(`
      def end class module if elsif else unless while until for in do return yield
      begin rescue ensure raise require require_relative attr_accessor attr_reader
      attr_writer self super new lambda proc then case when next break puts
    `),
    php: words(`
      function class interface trait extends implements public private protected
      static abstract final const return if else elseif foreach for while do break
      continue switch case default try catch finally throw new echo print use
      namespace require include global isset unset array
    `),
    swift: words(`
      func let var class struct enum protocol extension import return if else guard
      for while repeat switch case default break continue where in is as try catch
      throw throws defer init deinit self super static public private internal open
      final lazy weak unowned mutating some any nil
    `),
    kt: words(`
      fun val var class object interface data sealed enum companion import return
      if else when for while do break continue try catch finally throw is as in
      out by lazy override open abstract private public internal protected suspend
      init constructor this super null true false
    `),
    sh: words(`
      if then else elif fi for while until do done case esac function return in
      select time coproc break continue local export source alias unset readonly
      declare typeset shift trap exit eval exec set
    `),
    sql: words(`
      select from where insert into values update set delete create table alter
      drop add column index view join inner left right full outer on group by
      order having limit offset union all distinct as and or not null is like in
      between exists case when then else end primary key foreign references
      default constraint unique check cascade begin commit rollback with returning
    `),
  };

  // Commands worth colouring in a shell block: the ones a guest will actually
  // see on a slip's worth of work.
  const SH_COMMANDS = words(`
    echo cd ls cat grep sed awk find sort uniq head tail wc xargs kill ps chmod
    chown mkdir rmdir rm cp mv ln touch tar zip unzip curl wget ssh scp git gh npm
    npx yarn pnpm bun node deno python python3 pip pip3 pytest ruff black make
    cargo go rustc java javac docker kubectl psql redis-cli sqlite3 open code
    export which env sleep printf test true false
  `);

  function clike(keywords) {
    return [
      rule(/\/\/[^\n]*/y, "c"),
      rule(/\/\*[\s\S]*?\*\//y, "c"),
      rule(/`(?:[^`\\]|\\[\s\S])*`/y, "s"),
      rule(/"(?:[^"\\\n]|\\[\s\S])*"/y, "s"),
      rule(/'(?:[^'\\\n]|\\[\s\S])*'/y, "s"),
      rule(/0[xXbBoO][0-9a-fA-F_]+n?|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[nlLfFdDuU]?/y, "n"),
      rule(/[A-Za-z_$][\w$]*/y, ident(keywords)),
      WS,
      PUNCT,
    ];
  }

  const BUILDERS = {
    js: () => clike(KEYWORDS.js),
    go: () => [rule(/`[^`]*`/y, "s"), ...clike(KEYWORDS.go)],
    rust: () => [rule(/'[a-z_]\w*(?![\w'])/y, "l"), ...clike(KEYWORDS.rust)],
    java: () => clike(KEYWORDS.java),
    c: () => [rule(/^[ \t]*#[a-z]+/my, "k"), ...clike(KEYWORDS.c)],
    swift: () => clike(KEYWORDS.swift),
    kt: () => clike(KEYWORDS.kt),
    php: () => [rule(/<\?php|\?>/y, "k"), rule(/\$[A-Za-z_]\w*/y, "v"), ...clike(KEYWORDS.php)],

    py: () => [
      rule(/#[^\n]*/y, "c"),
      rule(/[rbfuRBFU]{0,2}('''[\s\S]*?'''|"""[\s\S]*?""")/y, "s"),
      rule(/[rbfuRBFU]{0,2}"(?:[^"\\\n]|\\[\s\S])*"/y, "s"),
      rule(/[rbfuRBFU]{0,2}'(?:[^'\\\n]|\\[\s\S])*'/y, "s"),
      rule(/@[A-Za-z_][\w.]*/y, "l"),
      rule(/0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?j?/y, "n"),
      rule(/[A-Za-z_][\w]*/y, ident(KEYWORDS.py)),
      WS,
      PUNCT,
    ],

    rb: () => [
      rule(/#[^\n]*/y, "c"),
      rule(/"(?:[^"\\\n]|\\[\s\S])*"|'(?:[^'\\\n]|\\[\s\S])*'/y, "s"),
      rule(/:[A-Za-z_]\w*[?!]?/y, "l"),
      rule(/[@$]{1,2}[A-Za-z_]\w*/y, "v"),
      rule(/\d[\d_]*(?:\.\d+)?/y, "n"),
      rule(/[A-Za-z_]\w*[?!]?/y, ident(KEYWORDS.rb)),
      WS,
      PUNCT,
    ],

    sh: () => [
      rule(/#[^\n]*/y, "c"),
      rule(/"(?:[^"\\]|\\[\s\S])*"/y, "s"),
      rule(/'[^']*'/y, "s"),
      rule(/\$\{[^}\n]*\}|\$[A-Za-z_]\w*|\$[@*#?!$0-9]/y, "v"),
      rule(/--?[A-Za-z][\w-]*/y, "l"),
      rule(/\b\d+\b/y, "n"),
      rule(
        /[A-Za-z_][\w.-]*/y,
        (text) => (KEYWORDS.sh.has(text) ? "k" : SH_COMMANDS.has(text) ? "f" : null)
      ),
      WS,
      PUNCT,
    ],

    json: () => [
      rule(/"(?:[^"\\]|\\[\s\S])*"(?=\s*:)/y, "v"),
      rule(/"(?:[^"\\]|\\[\s\S])*"/y, "s"),
      rule(/-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y, "n"),
      rule(/true|false|null/y, "l"),
      WS,
      PUNCT,
    ],

    html: () => [
      rule(/<!--[\s\S]*?-->/y, "c"),
      rule(/<!\[CDATA\[[\s\S]*?\]\]>/y, "s"),
      rule(/<!DOCTYPE[^>]*>/iy, "c"),
      rule(/<\/?[A-Za-z][\w:.-]*/y, "t"),
      rule(/"[^"\n]*"|'[^'\n]*'/y, "s"),
      rule(/&[a-zA-Z#0-9]+;/y, "n"),
      rule(/[A-Za-z_:][\w:.-]*(?=\s*=)/y, "l"),
      WS,
      PUNCT,
    ],

    css: () => [
      rule(/\/\*[\s\S]*?\*\//y, "c"),
      rule(/"[^"\n]*"|'[^'\n]*'/y, "s"),
      rule(/#[0-9a-fA-F]{3,8}\b/y, "n"),
      rule(/@[A-Za-z-]+/y, "k"),
      rule(/!important/y, "k"),
      rule(/[.#][A-Za-z_-][\w-]*/y, "t"),
      rule(/--[A-Za-z_-][\w-]*/y, "v"),
      rule(/[A-Za-z-]+(?=\s*:)/y, "l"),
      rule(/-?\d*\.?\d+(?:px|em|rem|ex|ch|vh|vw|vmin|vmax|%|s|ms|deg|fr|pt|cm|mm)?/y, "n"),
      WS,
      PUNCT,
    ],

    yaml: () => [
      rule(/#[^\n]*/y, "c"),
      rule(/"(?:[^"\\\n]|\\.)*"|'[^'\n]*'/y, "s"),
      rule(/[A-Za-z_][\w.\/-]*(?=\s*:)/y, "v"),
      rule(/[&*][A-Za-z_][\w-]*/y, "l"),
      rule(/\b(?:true|false|null|yes|no|on|off)\b/iy, "l"),
      rule(/-?\d+(?:\.\d+)?/y, "n"),
      WS,
      PUNCT,
    ],

    toml: () => [
      rule(/#[^\n]*|;[^\n]*/y, "c"),
      rule(/"""[\s\S]*?"""|"[^"\n]*"|'[^'\n]*'/y, "s"),
      rule(/^\s*\[[^\]\n]*\]/my, "t"),
      rule(/[A-Za-z_][\w.-]*(?=\s*=)/y, "v"),
      rule(/\b(?:true|false)\b/y, "l"),
      rule(/-?\d[\d_]*(?:\.\d+)?/y, "n"),
      WS,
      PUNCT,
    ],

    sql: () => [
      rule(/--[^\n]*/y, "c"),
      rule(/\/\*[\s\S]*?\*\//y, "c"),
      rule(/'(?:[^'\\]|\\.)*'/y, "s"),
      rule(/"[^"\n]*"|`[^`\n]*`/y, "v"),
      rule(/-?\d+(?:\.\d+)?/y, "n"),
      rule(/[A-Za-z_][\w$]*/y, ident(KEYWORDS.sql, true)),
      WS,
      PUNCT,
    ],

    md: () => [
      rule(/^#{1,6}[ \t][^\n]*/my, "k"),
      rule(/```[\s\S]*?```|`[^`\n]+`/y, "s"),
      rule(/\*\*[^*\n]+\*\*|__[^_\n]+__/y, "t"),
      rule(/\[[^\]\n]*\]\([^)\n]*\)/y, "f"),
      rule(/^[ \t]*(?:[-*+]|\d+\.)[ \t]/my, "l"),
      rule(/^[ \t]*>[^\n]*/my, "c"),
      WS,
      rule(/[^\s]/y, null),
    ],

    diff: () => [
      rule(/^\+[^\n]*/my, "add"),
      rule(/^-[^\n]*/my, "del"),
      rule(/^@@[^\n]*/my, "f"),
      rule(/^(?:diff|index|---|\+\+\+)[^\n]*/my, "c"),
      WS,
      rule(/[^\n]/y, null),
    ],
  };

  const ALIAS = {
    javascript: "js", jsx: "js", mjs: "js", cjs: "js", node: "js",
    typescript: "js", ts: "js", tsx: "js",
    python: "py", py3: "py", python3: "py",
    golang: "go", rs: "rust",
    shell: "sh", bash: "sh", zsh: "sh", console: "sh", terminal: "sh", ksh: "sh",
    cpp: "c", "c++": "c", cc: "c", h: "c", hpp: "c", cs: "java", csharp: "java",
    ruby: "rb", kotlin: "kt", kts: "kt",
    yml: "yaml", ini: "toml", cfg: "toml", conf: "toml", env: "toml",
    htm: "html", xml: "html", svg: "html", vue: "html",
    scss: "css", sass: "css", less: "css",
    markdown: "md", mdx: "md", patch: "diff",
    postgres: "sql", psql: "sql", mysql: "sql", sqlite: "sql",
  };

  const EXTENSIONS = {
    js: "js", mjs: "js", cjs: "js", jsx: "js", ts: "js", tsx: "js",
    py: "py", pyi: "py", rb: "rb", go: "go", rs: "rust",
    java: "java", kt: "kt", kts: "kt", swift: "swift", php: "php",
    c: "c", h: "c", cc: "c", cpp: "c", hpp: "c", cs: "java",
    sh: "sh", bash: "sh", zsh: "sh", fish: "sh",
    json: "json", jsonc: "json",
    html: "html", htm: "html", xml: "html", svg: "html", vue: "html",
    css: "css", scss: "css", sass: "css", less: "css",
    yaml: "yaml", yml: "yaml",
    toml: "toml", ini: "toml", cfg: "toml", conf: "toml",
    sql: "sql", md: "md", markdown: "md", diff: "diff", patch: "diff",
  };

  const cache = {};

  function rulesFor(lang) {
    const id = ALIAS[lang] || lang;
    if (!id || !BUILDERS[id]) return null;
    if (!cache[id]) cache[id] = BUILDERS[id]();
    return cache[id];
  }

  function normalise(lang) {
    const raw = String(lang || "").trim().toLowerCase().replace(/^\./, "");
    return ALIAS[raw] || raw;
  }

  // Dotfiles and extensionless names the salon actually sees.
  const BY_NAME = {
    dockerfile: "sh",
    makefile: "sh",
    ".gitignore": "sh",
    ".env": "toml",
    ".env.example": "toml",
  };

  function langFromPath(path) {
    const name = String(path || "").split("/").pop() || "";
    const lower = name.toLowerCase();
    if (BY_NAME[lower]) return BY_NAME[lower];
    const dot = lower.lastIndexOf(".");
    if (dot <= 0) return "";
    return EXTENSIONS[lower.slice(dot + 1)] || "";
  }

  function supports(lang) {
    return !!rulesFor(normalise(lang));
  }

  function highlight(code, lang) {
    const text = code == null ? "" : String(code);
    const rules = rulesFor(normalise(lang));
    if (!rules || text.length > MAX_CHARS) return esc(text);
    const key = normalise(lang) + " " + text;
    const small = text.length <= MEMO_CHARS;
    if (small) {
      const hit = memo.get(key);
      if (hit !== undefined) return hit;
    }
    let out = "";
    let plain = "";
    let i = 0;
    const n = text.length;
    while (i < n) {
      let matched = null;
      let cls = null;
      for (let r = 0; r < rules.length; r++) {
        const current = rules[r];
        current.re.lastIndex = i;
        const found = current.re.exec(text);
        if (!found || found.index !== i || found[0].length === 0) continue;
        matched = found[0];
        cls = typeof current.cls === "function" ? current.cls(matched, text, i + matched.length) : current.cls;
        break;
      }
      if (matched === null) {
        plain += text[i];
        i += 1;
        continue;
      }
      if (cls) {
        out += esc(plain);
        plain = "";
        out += '<span class="tk-' + cls + '">' + esc(matched) + "</span>";
      } else {
        plain += matched;
      }
      i += matched.length;
    }
    out += esc(plain);
    if (small) {
      if (memo.size > 400) memo.clear();
      memo.set(key, out);
    }
    return out;
  }

  global.HL = { highlight, langFromPath, supports, escape: esc };
})(typeof window !== "undefined" ? window : globalThis);
