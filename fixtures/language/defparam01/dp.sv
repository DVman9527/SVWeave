module dp_leaf;
  parameter int LEAF_ID = 0;
endmodule

module dp_top;
  for (genvar i = 0; i < 3; i++) begin : g
    dp_leaf u_leaf ();
    defparam g[i].u_leaf.LEAF_ID = i;
  end
endmodule
