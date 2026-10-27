module native_plain_top(input [3:0] d, output [3:0] q);
  native_plain_leaf u_plain(.d(d), .q(q));
endmodule
module native_plain_leaf(input [3:0] d, output [3:0] q);
  assign q = d;
endmodule
