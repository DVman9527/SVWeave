module leaf(input logic i, output logic o);
  assign o = i;
endmodule

module wrap(input logic i, output logic o);
  leaf y(.i(i), .o(o));
endmodule

module sem09_top(input logic [5:0] in, output logic [5:0] out);
  leaf \x.y (.i(in[0]), .o(out[0]));
  wrap x (.i(in[1]), .o(out[1]));
  for (genvar a = 0; a < 2; a++) begin : g
    for (genvar b = 0; b < 2; b++) begin : h
      leaf u (.i(in[2 + a*2 + b]), .o(out[2 + a*2 + b]));
    end
  end
endmodule
