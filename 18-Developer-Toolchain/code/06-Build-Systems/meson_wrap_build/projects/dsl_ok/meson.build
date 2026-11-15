project('dsl_ok', meson_version: '>=1.12.0')

# Every object is immutable: += builds a new array and rebinds only b.
a = [1]
b = a
b += [2]
message('immutability: a has @0@ element(s), b has @1@'.format(a.length(), b.length()))

# Dictionaries are typed values too; keys must be strings.
d = {'opt': 'O2'}
d += {'lto': 'thin'}
message('dict keys: ' + ' '.join(d.keys()))

# foreach is the only loop, and it only walks a finite array or dict.
total = 0
foreach i : [1, 2, 3, 4]
  if i == 3
    continue
  endif
  total += i
endforeach
message('foreach total: @0@'.format(total))
