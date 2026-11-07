.text
.globl caller
.type caller, %function
caller:
    adrp x0, target_var
    add x0, x0, :lo12:target_var
    ldr w0, [x0]
    ret
.size caller, .-caller

.globl do_call
.type do_call, %function
do_call:
    stp x29, x30, [sp, -16]!
    mov x29, sp
    bl target_func
    ldp x29, x30, [sp], 16
    ret
.size do_call, .-do_call

.section .note.GNU-stack,"",%progbits
