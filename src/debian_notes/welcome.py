"""The note shown on first launch."""

WELCOME_HTML = """\
<h1>Welcome to Notes</h1>\
<div>Notes is the best place to jot down quick thoughts or save longer notes filled with checklists, \
tables, images, sketches and more.</div>\
<div><br></div>\
<h2>Get started</h2>\
<ul class="checklist">\
<li data-checked="true">Open Notes</li>\
<li>Create a new note with the compose button or <b>Ctrl+N</b></li>\
<li>Make a checklist with <b>Ctrl+Shift+L</b> and tick items off</li>\
<li>Insert a table with <b>Ctrl+Alt+T</b></li>\
<li>Lock a private note with <b>Ctrl+L</b></li>\
</ul>\
<div><br></div>\
<h2>Format text</h2>\
<div>Use the <b>Aa</b> button to choose a <b>Title</b>, <b>Heading</b>, <b>Subheading</b>, Body or \
<span style="font-family: var(--mono-font)">Monostyled</span> text, plus <b>bold</b>, <i>italic</i>, \
<u>underline</u> and <s>strikethrough</s>.</div>\
<ul class="dashed"><li>Dashed lists</li><li>start with "- "</li></ul>\
<ol><li>Numbered lists</li><li>start with "1. "</li></ol>\
<blockquote>Block quotes keep important passages front and centre.</blockquote>\
<div><br></div>\
<h3>Organise with tags</h3>\
<div>Type a hashtag such as #welcome anywhere in a note and it appears under Tags in the sidebar.</div>\
<div><br></div>\
<table class="nt-table"><tbody>\
<tr><td><b>Shortcut</b></td><td><b>Action</b></td></tr>\
<tr><td>Ctrl+Shift+T / H / J / B</td><td>Title / Heading / Subheading / Body</td></tr>\
<tr><td>Ctrl+F</td><td>Search all notes</td></tr>\
<tr><td>Ctrl+Shift+N</td><td>New folder</td></tr>\
</tbody></table>\
<div><br></div>\
"""
