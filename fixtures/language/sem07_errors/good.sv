module err_top;
  logic [7:0] x;
  string s;
  int n;
  assign x = 8'h01;
  initial n = s;
  initial x = missing_sym;
  bad_syn u_syn();
endmodule
