// Synthetic, nonfunctional encryption envelope: no licensed IP or ciphertext.
module visible_before(input logic a, output logic b);
  assign b = a;
endmodule
`pragma protect begin_protected
`pragma protect data_method = "aes256-cbc"
`pragma protect data_block
U1lOVEhFVElDX09OTFk=
`pragma protect end_protected
module visible_after;
  encrypted_unknown u_unknown(); // Must NOT become an invented stub or compile success.
endmodule
