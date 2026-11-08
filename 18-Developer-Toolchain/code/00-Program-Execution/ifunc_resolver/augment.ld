SECTIONS
{
  .registry : {
    KEEP(*(.registry))
  }
}
INSERT AFTER .rodata;
