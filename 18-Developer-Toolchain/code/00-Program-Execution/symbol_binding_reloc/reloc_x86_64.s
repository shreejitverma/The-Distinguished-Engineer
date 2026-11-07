.text
.globl caller
.type caller, @function
caller:
    movl target_var(%rip), %eax
    ret
.size caller, .-caller

.globl do_call
.type do_call, @function
do_call:
    call target_func
    ret
.size do_call, .-do_call

.section .note.GNU-stack,"",@progbits
