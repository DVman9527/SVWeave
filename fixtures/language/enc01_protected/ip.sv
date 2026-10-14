// SYNTHETIC encrypted-IP envelope: the data block is filler text, not real IP.
module vendor_ip(input logic clk, input logic [7:0] d, output logic [7:0] q);
`pragma protect begin_protected
`pragma protect encrypt_agent="SYNTH"
`pragma protect data_method="aes128-cbc"
`pragma protect encoding=(enctype="base64", line_length=76, bytes=64)
`pragma protect data_block
QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0
NTY3ODk=
`pragma protect end_protected
endmodule
