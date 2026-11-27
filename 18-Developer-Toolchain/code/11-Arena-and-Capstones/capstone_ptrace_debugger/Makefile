CC ?= cc
CFLAGS ?= -Wall -Wextra -O2

all: target debugger

target: target.c
	$(CC) -g -O0 -o target target.c

debugger: debugger.c
	$(CC) $(CFLAGS) -o debugger debugger.c

clean:
	rm -f target debugger
